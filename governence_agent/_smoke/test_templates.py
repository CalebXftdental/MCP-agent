"""Local HTTP smoke for the governed reusable template registry."""
from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "templates"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "template_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "template_password",
    "GOVERNANCE_VIEWER_USER": "template_viewer",
    "GOVERNANCE_VIEWER_PASSWORD": "viewer_password",
    "GOVERNANCE_SESSION_SECRET": "template-smoke-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_TEMPLATE_STORE_FILE": str(TMP / "state" / "templates.json"),
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


sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))
gateway_app = importlib.import_module("app")

with TestClient(gateway_app.app, base_url="http://testserver") as client:
    admin_login = client.post("/dashboard/login", json={"username": "template_admin", "password": "template_password"})
    check("admin login succeeds", admin_login.status_code == 200, admin_login.text)

    create = client.post("/templates", json={
        "display_name": "Quarterly Account Review Deck",
        "template_type": "powerpoint",
        "description": "Reusable QBR deck structure for customer review workflows.",
        "classification": ["INTERNAL"],
        "tags": ["customer", "qbr"],
        "allowed_workflow_ids": ["customer_360_report"],
        "notes": "Initial version",
        "content": {"sections": [{"heading": "Executive Summary", "bullets": ["Current position", "Risks", "Next actions"]}]},
    })
    body = create.json() if create.headers.get("content-type", "").startswith("application/json") else {}
    check("admin creates template", create.status_code == 201 and body.get("templateId"), create.text)
    tid = body.get("templateId")
    check("template has v1", body.get("currentVersion") == 1 and len(body.get("versions") or []) == 1, body)

    listed = client.get("/templates")
    check("template listed", any(t.get("templateId") == tid for t in listed.json().get("templates", [])), listed.text)

    detail = client.get(f"/templates/{tid}?content=1")
    detail_body = detail.json()
    check("admin detail includes content", detail.status_code == 200 and detail_body.get("versions", [{}])[0].get("content"), detail.text)

    version = client.post(f"/templates/{tid}/versions", json={
        "notes": "Add agenda slide",
        "content": {"sections": [{"heading": "Agenda", "bullets": ["Status", "Open risks", "Actions"]}]},
    })
    version_body = version.json()
    check("admin adds version", version.status_code == 201 and version_body.get("currentVersion") == 2 and len(version_body.get("versions") or []) == 2, version.text)

    patched = client.patch(f"/templates/{tid}", json={"tags": ["customer", "qbr", "approved"], "status": "active"})
    check("admin updates metadata", patched.status_code == 200 and "approved" in patched.json().get("tags", []), patched.text)

    # Viewer can list and inspect active templates, but cannot mutate them.
    client.post("/dashboard/logout")
    viewer_login = client.post("/dashboard/login", json={"username": "template_viewer", "password": "viewer_password"})
    check("viewer login succeeds", viewer_login.status_code == 200, viewer_login.text)
    viewer_list = client.get("/templates")
    check("viewer sees active template", any(t.get("templateId") == tid for t in viewer_list.json().get("templates", [])), viewer_list.text)
    viewer_create = client.post("/templates", json={"display_name": "Bad", "template_type": "generic", "content": {}})
    check("viewer cannot create template", viewer_create.status_code == 403, viewer_create.text)

    client.post("/dashboard/logout")
    client.post("/dashboard/login", json={"username": "template_admin", "password": "template_password"})
    disabled = client.post(f"/templates/{tid}/disable")
    check("admin disables template", disabled.status_code == 200 and disabled.json().get("status") == "disabled", disabled.text)
    hidden = client.get("/templates")
    check("disabled hidden by default", all(t.get("templateId") != tid for t in hidden.json().get("templates", [])), hidden.text)
    visible_admin = client.get("/templates?include_disabled=1")
    check("admin can include disabled", any(t.get("templateId") == tid and t.get("status") == "disabled" for t in visible_admin.json().get("templates", [])), visible_admin.text)

    gateway_app.template_store.reload_for_tests()
    persisted = client.get(f"/templates/{tid}?content=1")
    check("template persists after reload", persisted.status_code == 200 and persisted.json().get("currentVersion") == 2, persisted.text)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
