"""Local HTTP smoke for artifact version history."""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import uuid
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "artifact-versions"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "ver_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "ver_password",
    "GOVERNANCE_SESSION_SECRET": "artifact-version-secret",
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
        print(f"  FAIL {name}: {detail}")


sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))
share_store = importlib.import_module("artifact_share_store")
share_store.reload_for_tests()
gateway_app = importlib.import_module("app")

print("artifact versions")
viewer_name = "ver_viewer_" + uuid.uuid4().hex[:8]
with TestClient(gateway_app.app, base_url="http://testserver") as client:
    login = client.post("/dashboard/login", json={"username": "ver_admin", "password": "ver_password"})
    admin_cookie = login.cookies.get("gov_session")
    admin_headers = {"cookie": f"gov_session={admin_cookie}"}
    client.cookies.clear()
    check("admin login succeeds", login.status_code == 200 and bool(admin_cookie), login.text)

    created_user = client.post("/admin/consumers", headers=admin_headers, json={
        "consumer_id": viewer_name,
        "name": viewer_name,
        "role": "user",
        "type": "user",
        "status": "active",
        "password": "viewer_password",
        "categories": ["orders"],
    })
    check("viewer account created", created_user.status_code == 201, created_user.text)
    viewer_login = client.post("/dashboard/login", json={"username": viewer_name, "password": "viewer_password"})
    viewer_cookie = viewer_login.cookies.get("gov_session")
    viewer_headers = {"cookie": f"gov_session={viewer_cookie}"}
    client.cookies.clear()
    check("viewer login succeeds", viewer_login.status_code == 200 and bool(viewer_cookie), viewer_login.text)

    upload = client.post("/artifacts", headers=admin_headers, json={
        "title": "Versioned note",
        "filename": "versioned-note.txt",
        "text": "original version",
        "classification": ["INTERNAL"],
    })
    artifact = upload.json()
    aid = artifact.get("artifactId")
    check("artifact upload succeeds", upload.status_code == 201 and aid and artifact.get("currentVersion") == 1, artifact)

    versions = client.get(f"/artifacts/{aid}/versions", headers=admin_headers)
    vbody = versions.json()
    check("original version listed", versions.status_code == 200 and len(vbody.get("versions", [])) == 1 and vbody["versions"][0].get("versionId") == "v1", vbody)

    v2 = client.post(f"/artifacts/{aid}/versions", headers=admin_headers, json={
        "filename": "versioned-note.txt",
        "text": "edited version",
        "note": "Edited in workbench",
        "mime_type": "text/plain; charset=utf-8",
    })
    v2body = v2.json()
    check("second version created", v2.status_code == 201 and v2body.get("version", {}).get("versionId") == "v2" and v2body.get("artifact", {}).get("currentVersion") == 2, v2body)

    latest = client.get(f"/artifacts/{aid}/download", headers=admin_headers)
    old = client.get(f"/artifacts/{aid}/versions/v1/download", headers=admin_headers)
    new = client.get(f"/artifacts/{aid}/versions/v2/download", headers=admin_headers)
    check("latest download is v2", latest.status_code == 200 and latest.content == b"edited version", latest.content)
    check("v1 download preserved", old.status_code == 200 and old.content == b"original version", old.content)
    check("v2 download works", new.status_code == 200 and new.content == b"edited version", new.content)

    meta = client.get(f"/artifacts/{aid}", headers=admin_headers).json()
    check("metadata tracks latest version", meta.get("currentVersion") == 2 and meta.get("versionCount") == 2 and meta.get("latestVersionId") == "v2", meta)

    viewer_blocked = client.get(f"/artifacts/{aid}/versions", headers=viewer_headers)
    check("viewer cannot list versions before share", viewer_blocked.status_code == 403, viewer_blocked.text)
    share = client.post(f"/artifacts/{aid}/shares", headers=admin_headers, json={"shared_with": viewer_name, "permissions": ["view"], "expires_in_days": 1})
    check("view-only share created", share.status_code == 201, share.text)
    viewer_versions = client.get(f"/artifacts/{aid}/versions", headers=viewer_headers)
    check("view-only share can list versions", viewer_versions.status_code == 200 and len(viewer_versions.json().get("versions", [])) == 2, viewer_versions.text)
    viewer_download_blocked = client.get(f"/artifacts/{aid}/versions/v2/download", headers=viewer_headers)
    check("view-only share cannot download version", viewer_download_blocked.status_code == 403, viewer_download_blocked.text)
    viewer_create_blocked = client.post(f"/artifacts/{aid}/versions", headers=viewer_headers, json={"text": "bad edit"})
    check("shared viewer cannot create version", viewer_create_blocked.status_code == 403, viewer_create_blocked.text)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
