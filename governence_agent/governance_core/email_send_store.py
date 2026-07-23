"""Durable send queue for approval-gated email delivery."""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import replace
from pathlib import Path

from email_send_models import EmailSendRecord

_SENDS: dict[str, EmailSendRecord] = {}
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
    configured = os.getenv("GOVERNANCE_EMAIL_SEND_STORE_FILE")
    if configured:
        return Path(configured).resolve()
    return _state_root() / "email-sends.json"


def _record_to_dict(r: EmailSendRecord) -> dict:
    return {
        "send_id": r.send_id,
        "owner": r.owner,
        "draft_artifact_id": r.draft_artifact_id,
        "approval_id": r.approval_id,
        "status": r.status,
        "provider": r.provider,
        "to": list(r.to),
        "cc": list(r.cc),
        "subject": r.subject,
        "attachment_artifact_ids": list(r.attachment_artifact_ids),
        "message": r.message,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "sent_at": r.sent_at,
    }


def _record_from_dict(d: dict) -> EmailSendRecord:
    return EmailSendRecord(
        send_id=d["send_id"],
        owner=d.get("owner", ""),
        draft_artifact_id=d.get("draft_artifact_id", ""),
        approval_id=d.get("approval_id", ""),
        status=d.get("status", "queued_for_connector"),
        provider=d.get("provider", "manual"),
        to=list(d.get("to") or []),
        cc=list(d.get("cc") or []),
        subject=d.get("subject", ""),
        attachment_artifact_ids=list(d.get("attachment_artifact_ids") or []),
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
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def create_send(*, owner: str, draft_artifact_id: str, approval_id: str, provider: str,
                to: list[str] | None = None, cc: list[str] | None = None, subject: str = "",
                attachment_artifact_ids: list[str] | None = None, status: str = "queued_for_connector",
                message: str = "") -> EmailSendRecord:
    _load()
    now = time.time()
    record = EmailSendRecord(
        send_id="send_" + uuid.uuid4().hex[:16],
        owner=owner,
        draft_artifact_id=draft_artifact_id,
        approval_id=approval_id,
        status=status,
        provider=provider or "manual",
        to=list(to or []),
        cc=list(cc or []),
        subject=subject,
        attachment_artifact_ids=list(attachment_artifact_ids or []),
        message=message,
        created_at=now,
        updated_at=now,
        sent_at=now if status == "sent" else None,
    )
    _SENDS[record.send_id] = record
    _save()
    return record


def list_sends(owner: str | None = None, limit: int = 100) -> list[EmailSendRecord]:
    _load()
    records = list(_SENDS.values())
    if owner:
        records = [r for r in records if r.owner == owner]
    records.sort(key=lambda r: r.created_at, reverse=True)
    return records[:limit]


def get_send(send_id: str) -> EmailSendRecord | None:
    _load()
    return _SENDS.get(send_id)


def update_send(send_id: str, **changes) -> EmailSendRecord | None:
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
