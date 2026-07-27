"""mcp-office -- governed office artifact generation backend.

This backend is intentionally data-blind: it only receives already-governed,
already-redacted structured content from the gateway/workflow layer and turns it
into office-compatible files. Artifact metadata is persisted through
governance_core.artifact_store.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
import zipfile
from xml.etree import ElementTree as ET
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.getenv("OFFICE_ENV_FILE") or ".env.local")

_CORE_DIR = str((Path(__file__).parent.parent / "governance_core").resolve())
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)
_APP_DIR = str(Path(__file__).parent.resolve())
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

import artifact_store
import onlyoffice_builder
from auth import onlyoffice_jwt
from builders.excel import build_xlsx
from builders.powerpoint import build_pptx
from builders.word import build_docx
from builders.pdf import build_pdf_packet


def _allowed_hosts() -> list[str]:
    raw = (os.getenv("OFFICE_ALLOWED_HOSTS") or "").strip()
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    for h in raw.split(","):
        h = h.strip()
        if h:
            hosts.extend((h, f"{h}:*"))
    return hosts


mcp = FastMCP(
    "frontier-mcp-office",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts(),
        allowed_origins=["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"],
    ),
)


def _result(record) -> str:
    payload = record.public_dict()
    payload["artifactStatus"] = payload.pop("status", "ready")
    return json.dumps({"source": "office", "status": "success", **payload})


def _classification(values: list[str] | None) -> list[str]:
    return sorted(set(values or ["INTERNAL"]))


_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
}


def _artifact_or_error(owner: str, artifact_id: str) -> tuple[object | None, dict | None]:
    record = artifact_store.get_artifact(artifact_id)
    if record is None:
        return None, {"source": "office", "status": "error", "errorCode": "artifact_not_found", "artifactId": artifact_id}
    if record.owner != owner:
        return None, {"source": "office", "status": "error", "errorCode": "forbidden", "artifactId": artifact_id}
    return record, None


def _read_payload(record) -> bytes:
    return Path(record.storage_path).read_bytes()


def _cell_text(cell, shared: list[str]) -> str:
    value = cell.find("s:v", _NS)
    text = value.text if value is not None and value.text is not None else ""
    if cell.get("t") == "s":
        try:
            return shared[int(text)]
        except (ValueError, IndexError):
            return text
    inline = cell.find("s:is/s:t", _NS)
    if inline is not None and inline.text:
        return inline.text
    return text


def _extract_xlsx(record) -> tuple[str, list[dict]]:
    shared: list[str] = []
    tables: list[dict] = []
    text_lines: list[str] = []
    with zipfile.ZipFile(record.storage_path) as zf:
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("s:si", _NS):
                shared.append("".join(t.text or "" for t in si.findall(".//s:t", _NS)))
        for name in sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")):
            root = ET.fromstring(zf.read(name))
            rows: list[list[str]] = []
            for row in root.findall(".//s:sheetData/s:row", _NS):
                values = [_cell_text(c, shared) for c in row.findall("s:c", _NS)]
                if any(v for v in values):
                    rows.append(values)
            if not rows:
                continue
            headers = rows[0]
            data = []
            for row in rows[1:]:
                item = {headers[i] if i < len(headers) and headers[i] else f"Column {i+1}": row[i] if i < len(row) else "" for i in range(max(len(headers), len(row)))}
                data.append(item)
            table = {"name": Path(name).stem, "rows": data or [{f"Column {i+1}": v for i, v in enumerate(rows[0])}]}
            tables.append(table)
            text_lines.append(table["name"])
            text_lines.extend([" | ".join(str(v) for v in r.values()) for r in table["rows"][:20]])
    return "\n".join(text_lines), tables


def _extract_docx(record) -> tuple[str, list[dict]]:
    text_lines: list[str] = []
    tables: list[dict] = []
    with zipfile.ZipFile(record.storage_path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
        for para in root.findall(".//w:p", _NS):
            line = "".join(t.text or "" for t in para.findall(".//w:t", _NS)).strip()
            if line:
                text_lines.append(line)
        for idx, tbl in enumerate(root.findall(".//w:tbl", _NS), start=1):
            rows: list[list[str]] = []
            for tr in tbl.findall("w:tr", _NS):
                cells = []
                for tc in tr.findall("w:tc", _NS):
                    cells.append(" ".join((t.text or "") for t in tc.findall(".//w:t", _NS)).strip())
                if any(cells):
                    rows.append(cells)
            if rows:
                headers = rows[0]
                data = []
                for row in rows[1:]:
                    data.append({headers[i] if i < len(headers) and headers[i] else f"Column {i+1}": row[i] if i < len(row) else "" for i in range(max(len(headers), len(row)))})
                tables.append({"name": f"Table {idx}", "rows": data or [{f"Column {i+1}": v for i, v in enumerate(rows[0])}]})
    return "\n".join(text_lines), tables


def _extract_pptx(record) -> tuple[str, list[dict]]:
    lines: list[str] = []
    with zipfile.ZipFile(record.storage_path) as zf:
        for name in sorted(n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")):
            root = ET.fromstring(zf.read(name))
            values = [t.text or "" for t in root.findall(".//a:t", _NS) if t.text]
            if values:
                lines.append(Path(name).stem + ": " + " | ".join(values))
    return "\n".join(lines), []


def _extract_text_and_tables(record) -> tuple[str, list[dict]]:
    suffix = Path(record.filename).suffix.lower()
    if suffix == ".xlsx" or record.type == "xlsx":
        return _extract_xlsx(record)
    if suffix == ".docx" or record.type == "docx":
        return _extract_docx(record)
    if suffix == ".pptx" or record.type == "pptx":
        return _extract_pptx(record)
    payload = _read_payload(record)
    if record.mime_type == "application/json" or suffix == ".json":
        try:
            parsed = json.loads(payload.decode("utf-8"))
            return json.dumps(parsed, indent=2, ensure_ascii=False), []
        except (UnicodeDecodeError, ValueError):
            pass
    try:
        return payload.decode("utf-8", errors="replace"), []
    except Exception:
        return f"Binary artifact {record.filename} ({record.size_bytes} bytes)", []


@mcp.tool()
async def create_excel_report(
    owner: str,
    title: str,
    tables: list[dict],
    classification: list[str] | None = None,
    filename: str = "",
) -> str:
    """Create an XLSX report from structured table data and return an artifact id."""
    out_name, payload, mime = build_xlsx(filename or title or "office-report", tables or [])
    record = artifact_store.create_artifact(
        owner=owner, title=title or out_name, filename=out_name, payload=payload,
        artifact_type="xlsx", mime_type=mime, classification=_classification(classification),
    )
    return _result(record)


@mcp.tool()
async def create_powerpoint_deck(
    owner: str,
    title: str,
    sections: list[dict],
    classification: list[str] | None = None,
    filename: str = "",
) -> str:
    """Create a PPTX deck from structured sections and return an artifact id."""
    out_name, payload, mime = build_pptx(filename or title or "office-deck", sections or [])
    record = artifact_store.create_artifact(
        owner=owner, title=title or out_name, filename=out_name, payload=payload,
        artifact_type="pptx", mime_type=mime, classification=_classification(classification),
    )
    return _result(record)


@mcp.tool()
async def create_word_report(
    owner: str,
    title: str,
    sections: list[dict],
    tables: list[dict] | None = None,
    classification: list[str] | None = None,
    filename: str = "",
) -> str:
    """Create a DOCX report from structured sections and tables and return an artifact id."""
    out_name, payload, mime = build_docx(filename or title or "office-report", sections or [], tables or [])
    record = artifact_store.create_artifact(
        owner=owner, title=title or out_name, filename=out_name, payload=payload,
        artifact_type="docx", mime_type=mime, classification=_classification(classification),
    )
    return _result(record)




@mcp.tool()
async def create_pdf_packet(
    owner: str,
    title: str,
    sections: list[dict],
    tables: list[dict] | None = None,
    classification: list[str] | None = None,
    filename: str = "",
) -> str:
    """Create a simple PDF review packet from structured sections/tables."""
    out_name, payload, mime = build_pdf_packet(filename or title or "office-packet", title or "Office Packet", sections or [], tables or [])
    record = artifact_store.create_artifact(
        owner=owner, title=title or out_name, filename=out_name, payload=payload,
        artifact_type="pdf", mime_type=mime, classification=_classification(classification),
    )
    return _result(record)


@mcp.tool()
async def convert_artifact(owner: str, artifact_id: str, target_format: str = "txt", title: str = "") -> str:
    """Convert a governed artifact into a review-friendly TXT or PDF artifact."""
    source, err = _artifact_or_error(owner, artifact_id)
    if err:
        return json.dumps(err)
    target = (target_format or "txt").strip().lower().lstrip(".")
    text, tables = _extract_text_and_tables(source)
    if target in ("txt", "text", "md"):
        out_name = f"{Path(source.filename).stem}.txt"
        payload = text.encode("utf-8")
        mime = "text/plain"
        artifact_type = "txt"
    elif target == "pdf":
        out_name, payload, mime = build_pdf_packet(
            f"{Path(source.filename).stem}-converted",
            title or f"Converted {source.filename}",
            [{"heading": "Source", "bullets": [source.filename]}, {"heading": "Extracted Text", "body": text[:12000]}],
            tables,
        )
        artifact_type = "pdf"
    else:
        return json.dumps({"source": "office", "status": "error", "errorCode": "unsupported_target_format", "targetFormat": target})
    record = artifact_store.create_artifact(
        owner=owner,
        title=title or f"Converted {source.title}",
        filename=out_name,
        payload=payload,
        artifact_type=artifact_type,
        mime_type=mime,
        classification=source.classification,
        source_artifact_ids=[source.artifact_id],
        source_tool_calls=["convert_artifact"],
    )
    return _result(record)


def _onlyoffice_server_url() -> str:
    return (os.getenv("ONLYOFFICE_DOCUMENT_SERVER_URL") or os.getenv("ONLYOFFICE_DOCSERVER_URL") or "").rstrip("/")


def _gateway_public_url() -> str:
    # mcp-office is a tool backend, not an HTTP handler -- it has no incoming
    # request to derive its own externally-reachable base URL from (unlike the
    # gateway's _onlyoffice_config, which uses request.base_url), so this has to
    # be configured explicitly.
    return (os.getenv("GATEWAY_PUBLIC_URL") or "").rstrip("/")


@mcp.tool()
async def edit_office_document(owner: str, artifact_id: str, edits: str) -> str:
    """Apply a small whitelisted set of edits to an existing governed XLSX/DOCX
    artifact via ONLYOFFICE Document Builder, and persist the result as a new
    artifact version. `edits` is a JSON-encoded string (like the `tables` param
    on create_excel_report) describing ops, e.g. for xlsx:
    '[{"op":"set_cell","sheet":"Sheet1","cell":"B4","value":"500"}]'; for docx:
    '[{"op":"replace_text","find":"TBD","replace":"Q3 2026"}]'.
    """
    source, err = _artifact_or_error(owner, artifact_id)
    if err:
        return json.dumps(err)
    server = _onlyoffice_server_url()
    gateway_base = _gateway_public_url()
    if not server or not gateway_base:
        return json.dumps({"source": "office", "status": "error", "errorCode": "onlyoffice_not_configured", "artifactId": artifact_id})
    try:
        ops = onlyoffice_builder.parse_ops(source.type, edits)
    except onlyoffice_builder.InvalidEdits as exc:
        return json.dumps({"source": "office", "status": "error", "errorCode": exc.error_code, "artifactId": artifact_id, **exc.detail})

    source_url = onlyoffice_jwt.scoped_download_url(gateway_base, source.artifact_id)
    script = onlyoffice_builder.build_script(source.type, source_url, source.filename, ops)
    script_record = artifact_store.create_artifact(
        owner=owner, title="Document Builder script (internal)", filename=f"docbuilder-{uuid.uuid4().hex}.js",
        payload=script.encode("utf-8"), artifact_type="docbuilder_script",
        mime_type="application/javascript", classification=["INTERNAL"], retention_days=1,
        source_artifact_ids=[source.artifact_id], source_tool_calls=["edit_office_document"],
    )
    script_url = onlyoffice_jwt.scoped_download_url(gateway_base, script_record.artifact_id, ttl_sec=300)

    body = {"async": False, "url": script_url}
    token = onlyoffice_jwt.sign(body)
    if token:
        body["token"] = token
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{server}/docbuilder", json=body)
            resp.raise_for_status()
            result = resp.json()
    except httpx.HTTPError as exc:
        return json.dumps({"source": "office", "status": "error", "errorCode": "docbuilder_request_failed", "detail": str(exc), "artifactId": artifact_id})
    finally:
        try:
            artifact_store.delete_artifact(script_record.artifact_id)
        except Exception:
            pass

    if result.get("error"):
        return json.dumps({"source": "office", "status": "error", "errorCode": "docbuilder_error", "docbuilderError": result.get("error"), "artifactId": artifact_id})
    if not result.get("end"):
        # Document Builder only supports polling by re-sending {"async":true,"key":...}
        # for long-running (async) jobs -- out of scope here since we always send
        # {"async": false}, so an incomplete sync response is treated as a failure
        # rather than adding polling logic for an edge case this scope doesn't hit.
        return json.dumps({"source": "office", "status": "error", "errorCode": "docbuilder_incomplete", "artifactId": artifact_id})
    urls = result.get("urls") or {}
    result_url = urls.get(source.filename) or (next(iter(urls.values())) if urls else None)
    if not result_url:
        return json.dumps({"source": "office", "status": "error", "errorCode": "docbuilder_no_output", "artifactId": artifact_id})

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(result_url)
            resp.raise_for_status()
            payload = resp.content
    except httpx.HTTPError as exc:
        return json.dumps({"source": "office", "status": "error", "errorCode": "docbuilder_download_failed", "detail": str(exc), "artifactId": artifact_id})

    version = artifact_store.create_version(
        artifact_id=source.artifact_id, payload=payload, filename=source.filename,
        mime_type=source.mime_type, created_by=owner, note="Edited via edit_office_document (AI)",
    )
    if version is None:
        return json.dumps({"source": "office", "status": "error", "errorCode": "artifact_not_found", "artifactId": artifact_id})
    latest = artifact_store.get_artifact(source.artifact_id) or source
    out = latest.public_dict()
    out["artifactStatus"] = out.pop("status", "ready")
    out["versionId"] = version.version_id
    out["editsApplied"] = ops
    return json.dumps({"source": "office", "status": "success", **out})


@mcp.tool()
async def extract_tables_from_document(owner: str, artifact_id: str, create_json_artifact: bool = False) -> str:
    """Extract table-like data from a governed XLSX/DOCX artifact."""
    source, err = _artifact_or_error(owner, artifact_id)
    if err:
        return json.dumps(err)
    text, tables = _extract_text_and_tables(source)
    out = {
        "source": "office",
        "status": "success",
        "sourceArtifactId": source.artifact_id,
        "filename": source.filename,
        "tableCount": len(tables),
        "tables": tables,
        "textPreview": text[:2000],
    }
    if create_json_artifact:
        payload = json.dumps({"sourceArtifactId": source.artifact_id, "tables": tables, "textPreview": text[:4000]}, indent=2, ensure_ascii=False).encode("utf-8")
        record = artifact_store.create_artifact(
            owner=owner,
            title=f"Extracted tables - {source.title}",
            filename=f"{Path(source.filename).stem}-tables.json",
            payload=payload,
            artifact_type="table_extract",
            mime_type="application/json",
            classification=source.classification,
            source_artifact_ids=[source.artifact_id],
            source_tool_calls=["extract_tables_from_document"],
        )
        out["artifact"] = record.public_dict()
    return json.dumps(out)


async def _health(_request):
    return JSONResponse({"ok": True, "service": "mcp-office"})


app = mcp.streamable_http_app()
app.add_route("/health", _health)


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("OFFICE_PORT") or "8030")
    print(f"[mcp-office] Starting on 0.0.0.0:{port} (path /mcp)")
    uvicorn.run(app, host="0.0.0.0", port=port)
