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

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

import artifact_store
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
