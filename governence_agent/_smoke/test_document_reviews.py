"""Local HTTP smoke for governed document review sessions on artifacts."""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import uuid
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "document-reviews"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "review_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "review_password",
    "GOVERNANCE_SESSION_SECRET": "document-review-smoke-secret",
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
review_store = importlib.import_module("document_review_store")
review_store.reload_for_tests()
gateway_app = importlib.import_module("app")

print("document review sessions")
viewer_name = "review_viewer_" + uuid.uuid4().hex[:8]
with TestClient(gateway_app.app, base_url="http://testserver") as client:
    login = client.post("/dashboard/login", json={"username": "review_admin", "password": "review_password"})
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
    check("viewer account created", created_user.status_code in (201, 409), created_user.text)
    viewer_login = client.post("/dashboard/login", json={"username": viewer_name, "password": "viewer_password"})
    viewer_cookie = viewer_login.cookies.get("gov_session")
    viewer_headers = {"cookie": f"gov_session={viewer_cookie}"}
    client.cookies.clear()
    check("viewer login succeeds", viewer_login.status_code == 200 and bool(viewer_cookie))

    upload = client.post("/artifacts", headers=admin_headers, json={
        "title": "Review packet",
        "filename": "review-packet.txt",
        "text": "Draft packet ready for collaborative review.",
        "classification": ["INTERNAL"],
    })
    artifact_id = upload.json().get("artifactId")
    check("artifact uploaded", upload.status_code == 201 and artifact_id, upload.text)

    blocked_create = client.post(f"/artifacts/{artifact_id}/reviews", headers=viewer_headers, json={"reason": "viewer tries"})
    check("non-owner cannot start review before share", blocked_create.status_code == 403)

    share = client.post(f"/artifacts/{artifact_id}/shares", headers=admin_headers, json={"shared_with": viewer_name, "permissions": ["view"], "expires_in_days": 1})
    check("artifact shared for view", share.status_code == 201, share.text)

    review = client.post(f"/artifacts/{artifact_id}/reviews", headers=admin_headers, json={"reason": "manager review", "provider": "local"})
    review_body = review.json()
    review_id = review_body.get("reviewId")
    check("owner starts review", review.status_code == 201 and review_id and review_body.get("status") == "open", review.text)
    check("review tracks latest version", review_body.get("artifactVersionId") == upload.json().get("latestVersionId"), review_body)

    viewer_reviews = client.get(f"/artifacts/{artifact_id}/reviews", headers=viewer_headers)
    check("shared viewer lists reviews", viewer_reviews.status_code == 200 and any(r.get("reviewId") == review_id for r in viewer_reviews.json().get("reviews", [])), viewer_reviews.text)

    comment = client.post(f"/artifacts/{artifact_id}/reviews/{review_id}/comments", headers=viewer_headers, json={"body": "Please tighten the executive summary.", "anchor": "summary"})
    check("shared viewer comments", comment.status_code == 200 and comment.json().get("commentCount") == 1, comment.text)

    empty_comment = client.post(f"/artifacts/{artifact_id}/reviews/{review_id}/comments", headers=viewer_headers, json={"body": ""})
    check("empty comments rejected", empty_comment.status_code == 400)

    viewer_decision = client.post(f"/artifacts/{artifact_id}/reviews/{review_id}/approve", headers=viewer_headers, json={"decision": "looks good"})
    check("non-admin cannot decide review", viewer_decision.status_code == 403)

    review_store.reload_for_tests()
    workbench = client.get(f"/artifacts/{artifact_id}/workbench", headers=admin_headers)
    reviews = workbench.json().get("reviews", [])
    persisted = next((r for r in reviews if r.get("reviewId") == review_id), {})
    check("workbench includes persisted review", workbench.status_code == 200 and persisted.get("commentCount") == 1, workbench.text)

    changes = client.post(f"/artifacts/{artifact_id}/reviews/{review_id}/changes", headers=admin_headers, json={"decision": "Revise summary and reroute."})
    changes_body = changes.json()
    check("admin requests changes", changes.status_code == 200 and changes_body.get("status") == "changes_requested" and changes_body.get("decidedBy") == "review_admin", changes.text)

    second = client.post(f"/artifacts/{artifact_id}/reviews", headers=admin_headers, json={"reason": "final review", "provider": "onlyoffice", "editorUrl": "http://onlyoffice.local/editor"})
    second_id = second.json().get("reviewId")
    approved = client.post(f"/artifacts/{artifact_id}/reviews/{second_id}/approve", headers=admin_headers, json={"decision": "Approved for delivery."})
    check("admin approves review", approved.status_code == 200 and approved.json().get("status") == "approved", approved.text)
    check("onlyoffice handoff fields persist", second.json().get("provider") == "onlyoffice" and second.json().get("editorUrl") == "http://onlyoffice.local/editor", second.text)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)