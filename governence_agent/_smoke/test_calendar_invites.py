"""Local HTTP smoke for governed calendar invite drafting and queueing."""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.testclient import TestClient

# Computed relative to "now" (not a hardcoded date) so "list upcoming meetings"
# stays inside its lookback/lookahead window regardless of when this runs.
_MEETING_START = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
_MEETING_START_ISO = _MEETING_START.isoformat().replace("+00:00", "Z")
_MEETING_END_ISO = (_MEETING_START + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "calendar"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "calendar_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "calendar_password",
    "GOVERNANCE_SESSION_SECRET": "calendar-smoke-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "CALENDAR_MCP_URL": "http://127.0.0.1:18060/mcp",
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


print("start calendar backend")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
if not python_exe.exists():
    python_exe = Path(sys.executable)
calendar = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18060", "--no-access-log"],
    cwd=str(ROOT / "mcp-calendar"),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18060/health")
    check("calendar backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")
    from policy import manifest
    from policy.categories import get_category

    check("calendar backend registered", "calendar" in manifest.backends())
    check("calendar draft category exists", get_category("calendar_draft") is not None)
    check("calendar send category exists", get_category("calendar_send_external") is not None)
    check("calendar read category exists", get_category("calendar") is not None)
    check("calendar read category grants meeting brief", "create_meeting_brief" in get_category("calendar").tools)
    check("calendar read category grants list meetings", "list_upcoming_meetings" in get_category("calendar").tools)
    check("calendar tool is namespaced", manifest.namespaced("draft_calendar_invite") == "calendar_draft_calendar_invite")

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "calendar_admin", "password": "calendar_password"})
        check("login succeeds", login.status_code == 200)

        draft = client.post("/dashboard/try-tool", json={
            "tool": "draft_calendar_invite",
            "args": {
                "title": "Customer Renewal Review",
                "start": _MEETING_START_ISO,
                "end": _MEETING_END_ISO,
                "timezone_name": "America/Toronto",
                "attendees": ["manager@example.com", "ae@example.com"],
                "location": "Teams",
                "description": "Review generated account packet and decide follow-up owner.",
                "classification": ["INTERNAL", "PII"],
            },
        })
        result = draft.json().get("result") or {}
        check("draft tool call succeeds", draft.status_code == 200)
        check("calendar draft created", result.get("status") == "success" and result.get("draftId"), result)
        draft_id = result["draftId"]

        blocked_send = client.post("/dashboard/try-tool", json={"tool": "send_calendar_invite", "args": {"draft_id": draft_id, "approval_id": "missing"}})
        blocked_result = blocked_send.json().get("result") or {}
        check("calendar send requires approval", blocked_send.status_code == 200 and blocked_result.get("status") == "approval_required", blocked_result)

        approval = client.post("/approvals", json={"reason": "Create external calendar event", "artifact_ids": [draft_id], "risk_level": "medium"})
        check("approval requested", approval.status_code == 201 and approval.json().get("status") == "pending", approval.text)
        approval_id = approval.json()["approvalId"]
        approved = client.post(f"/approvals/{approval_id}/approve", json={"note": "calendar smoke ok"})
        check("approval approved", approved.status_code == 200 and approved.json().get("status") == "approved", approved.text)
        gateway_app.approval_store.reload_for_tests()

        queued = client.post("/dashboard/try-tool", json={"tool": "send_calendar_invite", "args": {"draft_id": draft_id, "approval_id": approval_id}})
        queued_result = queued.json().get("result") or {}
        check("approved calendar invite queued", queued.status_code == 200 and queued_result.get("sendStatus") == "queued_for_connector" and queued_result.get("sendId"), queued.text)
        gateway_app.calendar_send_store.reload_for_tests()
        sends = client.get("/calendar-sends")
        check("calendar queue lists record", any(s.get("sendId") == queued_result.get("sendId") for s in sends.json().get("sends", [])), sends.text)
        send_item = client.get(f"/calendar-sends/{queued_result['sendId']}")
        check("calendar queue item persists", send_item.status_code == 200 and send_item.json().get("approvalId") == approval_id, send_item.text)

        workbench = client.get(f"/artifacts/{draft_id}/workbench")
        preview = workbench.json().get("preview") or {}
        check("calendar workbench loads", workbench.status_code == 200 and preview.get("signals", {}).get("title") == "Customer Renewal Review", workbench.text)
        download = client.get(result["downloadUrl"])
        check("ics artifact downloads", download.status_code == 200 and b"BEGIN:VCALENDAR" in download.content and b"Customer Renewal Review" in download.content)

        brief = client.post("/dashboard/try-tool", json={
            "tool": "create_meeting_brief",
            "args": {
                "title": "Prep: Customer Renewal Review",
                "meeting_time": _MEETING_START_ISO,
                "attendees": ["manager@example.com", "ae@example.com"],
                "sections": [{"heading": "Account status", "bullets": ["Spend up 12% QoQ", "One delayed shipment this month"]}],
                "source_artifact_ids": [draft_id],
                "classification": ["INTERNAL", "PII"],
            },
        })
        brief_result = brief.json().get("result") or {}
        check("meeting brief tool call succeeds", brief.status_code == 200, brief.text)
        check("meeting brief created", brief_result.get("status") == "success" and brief_result.get("artifactId"), brief_result)
        brief_download = client.get(brief_result.get("downloadUrl", ""))
        check("meeting brief downloads", brief_download.status_code == 200 and b"Account status" in brief_download.content, brief_download.text)

        upcoming = client.post("/dashboard/try-tool", json={"tool": "list_upcoming_meetings", "args": {"within_days": 30}})
        upcoming_result = upcoming.json().get("result") or {}
        check("list upcoming meetings succeeds", upcoming.status_code == 200 and upcoming_result.get("status") == "success", upcoming_result)
        check("list upcoming meetings finds the drafted invite", any(m.get("draftId") == draft_id for m in upcoming_result.get("meetings", [])), upcoming_result)
finally:
    calendar.terminate()
    try:
        calendar.wait(timeout=5)
    except subprocess.TimeoutExpired:
        calendar.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
