"""Local HTTP smoke for admin audit export artifacts."""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import uuid
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "audit-export"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "audit_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "audit_password",
    "GOVERNANCE_SESSION_SECRET": "audit-export-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
})

PASS, FAIL = 0, 0


def check(name, cond, detail=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}", detail if detail is not None else "")


sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))
gateway_app = importlib.import_module("app")

viewer = "audit_viewer_" + uuid.uuid4().hex[:8]
with TestClient(gateway_app.app, base_url="http://testserver") as client:
    admin_login = client.post("/dashboard/login", json={"username": "audit_admin", "password": "audit_password"})
    check("admin login succeeds", admin_login.status_code == 200, admin_login.text)

    created = client.post("/admin/consumers", json={
        "name": viewer,
        "full_name": "Audit Viewer",
        "password": "viewer_password",
        "role": "user",
        "type": "user",
        "categories": ["orders"],
    })
    check("viewer created", created.status_code == 201, created.text)

    calls = client.get("/admin/calls?limit=5")
    check("admin calls endpoint available", calls.status_code == 200, calls.text)

    export = client.post("/admin/audit-export", json={"hours": 24, "type": "policy_change", "limit": 200})
    export_body = export.json()
    artifact = export_body.get("artifact") or {}
    check("admin exports audit artifact", export.status_code == 201 and artifact.get("type") == "json", export.text)
    check("export summary counts policy changes", export_body.get("summary", {}).get("total", 0) >= 1, export_body)

    download = client.get(artifact.get("downloadUrl", ""))
    packet = download.json()
    check("audit export downloads", download.status_code == 200 and packet.get("kind") == "audit_export", download.text)
    check("audit export stores filters", packet.get("filters", {}).get("type") == "policy_change" and packet.get("filters", {}).get("hours") == 24, packet.get("filters"))
    check("audit export records are filtered", all(r.get("type") == "policy_change" for r in packet.get("records", [])), packet.get("records"))

    client.post("/dashboard/logout")
    viewer_login = client.post("/dashboard/login", json={"username": viewer, "password": "viewer_password"})
    check("viewer login succeeds", viewer_login.status_code == 200, viewer_login.text)
    denied = client.post("/admin/audit-export", json={"hours": 24})
    check("viewer cannot export audit", denied.status_code == 403, denied.text)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)