"""Local HTTP smoke for governed artifact file operations."""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import uuid
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "artifact-file-ops"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "file_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "file_password",
    "GOVERNANCE_SESSION_SECRET": "artifact-file-ops-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
})

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))
artifact_store = importlib.import_module("artifact_store")
share_store = importlib.import_module("artifact_share_store")
share_store.reload_for_tests()
gateway_app = importlib.import_module("app")

print("artifact file operations")
viewer_name = "file_viewer_" + uuid.uuid4().hex[:8]
with TestClient(gateway_app.app, base_url="http://testserver") as client:
    login = client.post("/dashboard/login", json={"username": "file_admin", "password": "file_password"})
    admin_cookie = login.cookies.get("gov_session")
    admin_headers = {"cookie": f"gov_session={admin_cookie}"}
    client.cookies.clear()
    check("admin login succeeds", login.status_code == 200 and bool(admin_cookie))

    created_user = client.post("/admin/consumers", headers=admin_headers, json={
        "consumer_id": viewer_name,
        "name": viewer_name,
        "role": "user",
        "type": "user",
        "status": "active",
        "password": "viewer_password",
        "categories": ["orders"],
    })
    check("viewer account created", created_user.status_code in (201, 409))
    viewer_login = client.post("/dashboard/login", json={"username": viewer_name, "password": "viewer_password"})
    viewer_cookie = viewer_login.cookies.get("gov_session")
    viewer_headers = {"cookie": f"gov_session={viewer_cookie}"}
    client.cookies.clear()
    check("viewer login succeeds", viewer_login.status_code == 200 and bool(viewer_cookie))

    upload = client.post("/artifacts", headers=admin_headers, json={
        "title": "Customer note",
        "filename": "customer-note.txt",
        "text": "Customer ABC has a pending executive report.",
        "classification": ["SENSITIVE"],
    })
    body = upload.json()
    aid = body.get("artifactId")
    check("upload succeeds", upload.status_code == 201 and aid)
    check("retention assigned from classification", body.get("retentionDays") == 60 and body.get("expiresAt"))

    admin_list = client.get("/artifacts", headers=admin_headers)
    check("owner lists uploaded artifact", any(a.get("artifactId") == aid for a in admin_list.json().get("artifacts", [])))
    viewer_list = client.get("/artifacts", headers=viewer_headers)
    check("viewer cannot list before share", aid not in [a.get("artifactId") for a in viewer_list.json().get("artifacts", [])])
    viewer_blocked = client.get(f"/artifacts/{aid}", headers=viewer_headers)
    check("viewer metadata blocked before share", viewer_blocked.status_code == 403)

    share = client.post(f"/artifacts/{aid}/shares", headers=admin_headers, json={"shared_with": viewer_name, "permissions": ["view", "download"], "expires_in_days": 1})
    share_body = share.json()
    sid = share_body.get("shareId")
    check("share created", share.status_code == 201 and sid and share_body.get("sharedWith") == viewer_name)

    viewer_list2 = client.get("/artifacts", headers=viewer_headers)
    check("viewer lists shared artifact", any(a.get("artifactId") == aid and a.get("shared") for a in viewer_list2.json().get("artifacts", [])))
    viewer_meta = client.get(f"/artifacts/{aid}", headers=viewer_headers)
    check("viewer can read shared metadata", viewer_meta.status_code == 200 and viewer_meta.json().get("artifactId") == aid)
    viewer_download = client.get(f"/artifacts/{aid}/download", headers=viewer_headers)
    check("viewer can download with permission", viewer_download.status_code == 200 and b"Customer ABC" in viewer_download.content)
    viewer_delete = client.delete(f"/artifacts/{aid}", headers=viewer_headers)
    check("viewer cannot delete shared artifact", viewer_delete.status_code == 403)

    shares = client.get(f"/artifacts/{aid}/shares", headers=admin_headers)
    check("owner can list shares", any(s.get("shareId") == sid for s in shares.json().get("shares", [])))
    revoke = client.post(f"/artifacts/{aid}/shares/{sid}/revoke", headers=admin_headers)
    check("share revoked", revoke.status_code == 200 and revoke.json().get("status") == "revoked")
    viewer_after_revoke = client.get(f"/artifacts/{aid}", headers=viewer_headers)
    check("viewer blocked after revoke", viewer_after_revoke.status_code == 403)

    delete = client.delete(f"/artifacts/{aid}", headers=admin_headers)
    check("owner deletes artifact", delete.status_code == 200 and delete.json().get("ok") is True)
    gone = client.get(f"/artifacts/{aid}", headers=admin_headers)
    check("deleted artifact is gone", gone.status_code == 404)

    expired = artifact_store.create_artifact(
        owner="file_admin",
        title="Expired note",
        filename="expired.txt",
        payload=b"old",
        artifact_type="txt",
        mime_type="text/plain",
        classification=["INTERNAL"],
        retention_days=0,
    )
    expired_meta = client.get(f"/artifacts/{expired.artifact_id}", headers=admin_headers)
    check("expired artifact returns gone", expired_meta.status_code == 410)
    purge = client.post("/admin/artifacts/purge-expired", headers=admin_headers)
    check("admin purges expired artifacts", purge.status_code == 200 and expired.artifact_id in [a.get("artifactId") for a in purge.json().get("purged", [])])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
