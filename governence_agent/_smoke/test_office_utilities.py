"""Local HTTP smoke for governed office artifact utility tools."""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "office_utilities"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "office_util_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "office_util_password",
    "GOVERNANCE_SESSION_SECRET": "office-util-smoke-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18035/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "10",
})

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def wait_health(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"service did not become healthy: {last}")


print("start office backend")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
if not python_exe.exists():
    python_exe = Path(sys.executable)
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18035", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18035/health")
    check("office backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")
    from policy import manifest

    check("pdf tool namespaced", manifest.namespaced("create_pdf_packet") == "office_create_pdf_packet")
    check("convert tool namespaced", manifest.namespaced("convert_artifact") == "office_convert_artifact")
    check("extract tool namespaced", manifest.namespaced("extract_tables_from_document") == "office_extract_tables_from_document")

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "office_util_admin", "password": "office_util_password"})
        check("login succeeds", login.status_code == 200, login.text)

        excel = client.post("/dashboard/try-tool", json={
            "tool": "create_excel_report",
            "args": {
                "title": "Utility Source Workbook",
                "filename": "utility-source.xlsx",
                "tables": [{"name": "Summary", "rows": [{"Customer": "ABC", "Total": 10}, {"Customer": "XYZ", "Total": 20}]}],
                "classification": ["INTERNAL", "SENSITIVE"],
            },
        })
        excel_body = excel.json().get("result", {})
        aid = excel_body.get("artifactId")
        check("source xlsx created", excel.status_code == 200 and aid and excel_body.get("filename", "").endswith(".xlsx"), excel.text)

        extracted = client.post("/dashboard/try-tool", json={
            "tool": "extract_tables_from_document",
            "args": {"artifact_id": aid, "create_json_artifact": True},
        })
        ex_body = extracted.json().get("result", {})
        check("tables extracted", extracted.status_code == 200 and ex_body.get("tableCount", 0) >= 1, extracted.text)
        check("extract artifact created", ex_body.get("artifact", {}).get("artifactId"), ex_body)

        txt = client.post("/dashboard/try-tool", json={
            "tool": "convert_artifact",
            "args": {"artifact_id": aid, "target_format": "txt"},
        })
        txt_body = txt.json().get("result", {})
        check("artifact converted to txt", txt.status_code == 200 and txt_body.get("filename", "").endswith(".txt"), txt.text)
        check("conversion preserves source lineage", aid in (txt_body.get("sourceArtifactIds") or []), txt_body)

        pdf = client.post("/dashboard/try-tool", json={
            "tool": "create_pdf_packet",
            "args": {
                "title": "Review Packet",
                "sections": [{"heading": "Summary", "bullets": ["Created from governed data", "Ready for review"]}],
                "tables": [{"name": "Metrics", "rows": [{"Metric": "Open", "Value": 3}]}],
                "classification": ["INTERNAL"],
            },
        })
        pdf_body = pdf.json().get("result", {})
        pdf_id = pdf_body.get("artifactId")
        check("pdf packet created", pdf.status_code == 200 and pdf_body.get("mimeType") == "application/pdf", pdf.text)
        download = client.get(f"/artifacts/{pdf_id}/download")
        check("pdf downloads with signature", download.status_code == 200 and download.content.startswith(b"%PDF-1.4"), str(download.status_code))
finally:
    office.terminate()
    try:
        office.wait(timeout=5)
    except subprocess.TimeoutExpired:
        office.kill()

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
