"""mcp-calendar -- governed calendar draft and connector queue backend."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.getenv("CALENDAR_ENV_FILE") or ".env.local")

_CORE_DIR = str((Path(__file__).parent.parent / "governance_core").resolve())
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

import approval_store
import artifact_store
import calendar_send_store


def _allowed_hosts() -> list[str]:
    raw = (os.getenv("CALENDAR_ALLOWED_HOSTS") or "").strip()
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    for h in raw.split(","):
        h = h.strip()
        if h:
            hosts.extend((h, f"{h}:*"))
    return hosts


mcp = FastMCP(
    "frontier-mcp-calendar",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts(),
        allowed_origins=["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"],
    ),
)


def _classification(values: list[str] | None) -> list[str]:
    return sorted(set(values or ["INTERNAL"]))


def _escape_ics(value: str) -> str:
    text = str(value or "")
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value or "calendar-invite").strip("-").lower()
    return (slug or "calendar-invite")[:60]


def _ics_time(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.strftime("%Y%m%dT%H%M%S")
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except ValueError:
        return raw.replace("-", "").replace(":", "")


def _build_ics(*, title: str, start: str, end: str, timezone_name: str, attendees: list[str], location: str, description: str) -> bytes:
    uid = f"gov-{abs(hash((title, start, end))) % 10**12}@frontier-governance"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Frontier Governance//Office Assistant//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{_ics_time(start)}",
        f"DTEND:{_ics_time(end)}",
        f"SUMMARY:{_escape_ics(title)}",
        f"LOCATION:{_escape_ics(location)}",
        f"DESCRIPTION:{_escape_ics(description)}",
        f"X-GOVERNANCE-TIMEZONE:{_escape_ics(timezone_name or 'UTC')}",
    ]
    for attendee in attendees or []:
        lines.append(f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{_escape_ics(attendee)}")
    lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
    return "\r\n".join(lines).encode("utf-8")


@mcp.tool()
async def draft_calendar_invite(
    owner: str,
    title: str,
    start: str,
    end: str,
    attendees: list[str],
    timezone_name: str = "UTC",
    location: str = "",
    description: str = "",
    classification: list[str] | None = None,
) -> str:
    """Create a durable .ics calendar invite draft. Does not create an external event."""
    payload = _build_ics(title=title, start=start, end=end, timezone_name=timezone_name, attendees=list(attendees or []), location=location, description=description)
    record = artifact_store.create_artifact(
        owner=owner,
        title=title or "Calendar invite",
        filename=f"{_slug(title)}.ics",
        payload=payload,
        artifact_type="calendar_invite",
        mime_type="text/calendar",
        classification=_classification(classification),
    )
    meta = {
        "title": title,
        "start": start,
        "end": end,
        "timezone": timezone_name or "UTC",
        "attendees": list(attendees or []),
        "location": location,
        "description": description,
    }
    sidecar = Path(record.storage_path).with_suffix(Path(record.storage_path).suffix + ".json")
    sidecar.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    out = record.public_dict()
    out["artifactStatus"] = out.pop("status", "ready")
    return json.dumps({"source": "calendar", "status": "success", "draftId": record.artifact_id, **out, **meta})


@mcp.tool()
async def send_calendar_invite(owner: str, draft_id: str, approval_id: str = "") -> str:
    """Queue an approved calendar invite for the configured calendar connector."""
    record = artifact_store.get_artifact(draft_id)
    if record is None or record.type != "calendar_invite":
        return json.dumps({"source": "calendar", "status": "error", "errorCode": "draft_not_found", "draftId": draft_id})
    if record.owner != owner:
        return json.dumps({"source": "calendar", "status": "error", "errorCode": "forbidden", "draftId": draft_id})
    approval_store.reload_for_tests()
    approval = approval_store.get_approval(approval_id)
    if approval is None or approval.status != "approved" or draft_id not in approval.artifact_ids:
        return json.dumps({
            "source": "calendar",
            "status": "approval_required",
            "draftId": draft_id,
            "approvalId": approval_id,
            "message": "An approved approval record tied to this calendar draft is required before queueing external invite creation.",
        })
    sidecar = Path(record.storage_path).with_suffix(Path(record.storage_path).suffix + ".json")
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {"title": record.title, "start": "", "end": "", "timezone": "UTC", "attendees": [], "location": ""}
    provider = (os.getenv("CALENDAR_DELIVERY_PROVIDER") or "manual").strip().lower() or "manual"
    mock_send = (os.getenv("CALENDAR_MOCK_SEND") or "false").strip().lower() in ("1", "true", "yes", "on")
    status = "sent" if mock_send else "queued_for_connector"
    message = "Mock invite creation completed." if mock_send else "Queued for the configured calendar connector; no external event was created by this local adapter."
    send = calendar_send_store.create_send(
        owner=owner,
        draft_artifact_id=draft_id,
        approval_id=approval_id,
        provider=provider,
        title=str(meta.get("title") or record.title),
        start=str(meta.get("start") or ""),
        end=str(meta.get("end") or ""),
        timezone=str(meta.get("timezone") or "UTC"),
        attendees=list(meta.get("attendees") or []),
        location=str(meta.get("location") or ""),
        status=status,
        message=message,
    )
    return json.dumps({
        "source": "calendar",
        "status": "success",
        "sendStatus": send.status,
        "sendId": send.send_id,
        "draftId": draft_id,
        "approvalId": approval_id,
        "provider": send.provider,
        "message": send.message,
    })


@mcp.tool()
async def create_meeting_brief(
    owner: str,
    title: str,
    meeting_time: str = "",
    attendees: list[str] | None = None,
    sections: list[dict] | None = None,
    source_artifact_ids: list[str] | None = None,
    classification: list[str] | None = None,
    filename: str = "",
) -> str:
    """Create a durable meeting-prep brief (markdown) from already-governed sections
    (e.g. a resolved customer overview + recent orders/shipments) -- this tool does not
    fetch business data itself; the caller assembles `sections` from prior, separately
    governed tool calls, same as the office report builders."""
    lines = [f"# {title}", ""]
    if meeting_time:
        lines.append(f"**When:** {meeting_time}")
    if attendees:
        lines.append(f"**Attendees:** {', '.join(attendees)}")
    if meeting_time or attendees:
        lines.append("")
    for section in (sections or []):
        heading = str(section.get("heading") or section.get("title") or "Notes")
        lines.append(f"## {heading}")
        for bullet in (section.get("bullets") or []):
            lines.append(f"- {bullet}")
        lines.append("")
    text = ("\n".join(lines)).strip() + "\n"
    record = artifact_store.create_artifact(
        owner=owner,
        title=title or "Meeting brief",
        filename=filename or f"{_slug(title)}-brief.md",
        payload=text.encode("utf-8"),
        artifact_type="meeting_brief",
        mime_type="text/markdown; charset=utf-8",
        classification=_classification(classification),
        source_artifact_ids=list(source_artifact_ids or []),
    )
    out = record.public_dict()
    out["artifactStatus"] = out.pop("status", "ready")
    return json.dumps({"source": "calendar", "status": "success", **out})


@mcp.tool()
async def list_upcoming_meetings(owner: str, within_days: int = 14, limit: int = 20) -> str:
    """List this owner's drafted calendar invites starting within the given window
    (reads the .ics drafts created by draft_calendar_invite -- there is no external
    calendar connector wired up in this local adapter)."""
    now_ts = datetime.now(timezone.utc).timestamp()
    horizon_ts = now_ts + max(1, int(within_days or 14)) * 86400
    meetings = []
    for record in artifact_store.list_artifacts(owner=owner, limit=500):
        if record.type != "calendar_invite":
            continue
        sidecar = Path(record.storage_path).with_suffix(Path(record.storage_path).suffix + ".json")
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        try:
            start_dt = datetime.fromisoformat(str(meta.get("start") or "").replace("Z", "+00:00"))
            start_ts = start_dt.timestamp() if start_dt.tzinfo else start_dt.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if now_ts <= start_ts <= horizon_ts:
            meetings.append({
                "draftId": record.artifact_id,
                "title": meta.get("title") or record.title,
                "start": meta.get("start"),
                "end": meta.get("end"),
                "attendees": meta.get("attendees") or [],
                "location": meta.get("location") or "",
            })
    meetings.sort(key=lambda m: m.get("start") or "")
    limit = max(1, int(limit or 20))
    return json.dumps({"source": "calendar", "status": "success", "meetings": meetings[:limit]})


async def _health(_request):
    return JSONResponse({"ok": True, "service": "mcp-calendar"})


app = mcp.streamable_http_app()
app.add_route("/health", _health)


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("CALENDAR_PORT") or "8060")
    print(f"[mcp-calendar] Starting on 0.0.0.0:{port} (path /mcp)")
    uvicorn.run(app, host="0.0.0.0", port=port)
