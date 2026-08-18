"""Durable send queue for approval-gated calendar delivery."""
from __future__ import annotations

import json
import os
import store_concurrency
import time
import uuid
from dataclasses import replace
from pathlib import Path

from calendar_send_models import CalendarSendRecord

_SENDS: dict[str, CalendarSendRecord] = {}
_LOADED = False


def _state_root() -> Path:
    configured = os.getenv("GOVERNANCE_STATE_DIR")
    if configured:
        return Path(configured).resolve()
    artifact_dir = os.getenv("GOVERNANCE_ARTIFACT_DIR")
    if artifact_dir:
        return (Path(artifact_dir).resolve().parent / "state").resolve()
    if Path("/home/data").exists():
        return Path("/home/data/governance-state").resolve()
    return Path("/tmp/governance-state").resolve()


def _store_file() -> Path:
    configured = os.getenv("GOVERNANCE_CALENDAR_SEND_STORE_FILE")
    if configured:
        return Path(configured).resolve()
    return _state_root() / "calendar-sends.json"


def _record_to_dict(r: CalendarSendRecord) -> dict:
    return {
        "send_id": r.send_id,
        "owner": r.owner,
        "draft_artifact_id": r.draft_artifact_id,
        "approval_id": r.approval_id,
        "status": r.status,
        "provider": r.provider,
        "title": r.title,
        "start": r.start,
        "end": r.end,
        "timezone": r.timezone,
        "attendees": list(r.attendees),
        "location": r.location,
        "message": r.message,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "sent_at": r.sent_at,
    }


def _record_from_dict(d: dict) -> CalendarSendRecord:
    return CalendarSendRecord(
        send_id=d["send_id"],
        owner=d.get("owner", ""),
        draft_artifact_id=d.get("draft_artifact_id", ""),
        approval_id=d.get("approval_id", ""),
        status=d.get("status", "queued_for_connector"),
        provider=d.get("provider", "manual"),
        title=d.get("title", ""),
        start=d.get("start", ""),
        end=d.get("end", ""),
        timezone=d.get("timezone", "UTC"),
        attendees=list(d.get("attendees") or []),
        location=d.get("location", ""),
        message=d.get("message", ""),
        created_at=float(d.get("created_at") or 0.0),
        updated_at=float(d.get("updated_at") or 0.0),
        sent_at=d.get("sent_at"),
    )


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    path = _store_file()
    _SENDS.clear()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw.get("sends", []):
                record = _record_from_dict(item)
                _SENDS[record.send_id] = record
        except (OSError, ValueError, KeyError, TypeError):
            _SENDS.clear()
    _LOADED = True


def _save() -> None:
    path = _store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "sends": [_record_to_dict(r) for r in sorted(_SENDS.values(), key=lambda x: x.created_at)]}
    store_concurrency.atomic_write_text(path, json.dumps(payload, indent=2))


def create_send(*, owner: str, draft_artifact_id: str, approval_id: str, provider: str,
                title: str = "", start: str = "", end: str = "", timezone: str = "UTC",
                attendees: list[str] | None = None, location: str = "",
                status: str = "queued_for_connector", message: str = "") -> CalendarSendRecord:
    _load()
    now = time.time()
    record = CalendarSendRecord(
        send_id="cal_" + uuid.uuid4().hex[:16],
        owner=owner,
        draft_artifact_id=draft_artifact_id,
        approval_id=approval_id,
        status=status,
        provider=provider or "manual",
        title=title,
        start=start,
        end=end,
        timezone=timezone or "UTC",
        attendees=list(attendees or []),
        location=location,
        message=message,
        created_at=now,
        updated_at=now,
        sent_at=now if status == "sent" else None,
    )
    _SENDS[record.send_id] = record
    _save()
    return record


def list_sends(owner: str | None = None, limit: int = 100) -> list[CalendarSendRecord]:
    _load()
    records = list(_SENDS.values())
    if owner:
        records = [r for r in records if r.owner == owner]
    records.sort(key=lambda r: r.created_at, reverse=True)
    return records[:limit]


def get_send(send_id: str) -> CalendarSendRecord | None:
    _load()
    return _SENDS.get(send_id)


def update_send(send_id: str, **changes) -> CalendarSendRecord | None:
    _load()
    existing = _SENDS.get(send_id)
    if not existing:
        return None
    changes.setdefault("updated_at", time.time())
    updated = replace(existing, **changes)
    _SENDS[send_id] = updated
    _save()
    return updated


def reload_for_tests() -> None:
    global _LOADED
    _LOADED = False
    _SENDS.clear()
