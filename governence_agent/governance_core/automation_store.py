"""Durable schedule store for recurring workflow automations."""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import replace
from pathlib import Path

from automation_models import AutomationSchedule

_AUTOMATIONS: dict[str, AutomationSchedule] = {}
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
    configured = os.getenv("GOVERNANCE_AUTOMATION_STORE_FILE")
    if configured:
        return Path(configured).resolve()
    return _state_root() / "automations.json"


def _record_to_dict(a: AutomationSchedule) -> dict:
    return {
        "automation_id": a.automation_id,
        "owner": a.owner,
        "template_id": a.template_id,
        "actor_type": a.actor_type,
        "agent_id": a.agent_id,
        "display_name": a.display_name,
        "inputs": dict(a.inputs),
        "interval_sec": a.interval_sec,
        "status": a.status,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
        "next_run_at": a.next_run_at,
        "last_run_at": a.last_run_at,
        "last_run_id": a.last_run_id,
        "last_status": a.last_status,
        "run_count": a.run_count,
    }


def _record_from_dict(d: dict) -> AutomationSchedule:
    return AutomationSchedule(
        automation_id=d["automation_id"],
        owner=d.get("owner", ""),
        template_id=d.get("template_id", ""),
        actor_type=d.get("actor_type", "agent" if d.get("agent_id") else "user"),
        agent_id=d.get("agent_id", ""),
        display_name=d.get("display_name", ""),
        inputs=dict(d.get("inputs") or {}),
        interval_sec=max(60, int(d.get("interval_sec") or 86400)),
        status=d.get("status", "active"),
        created_at=float(d.get("created_at") or 0.0),
        updated_at=float(d.get("updated_at") or 0.0),
        next_run_at=float(d.get("next_run_at") or 0.0),
        last_run_at=d.get("last_run_at"),
        last_run_id=d.get("last_run_id", ""),
        last_status=d.get("last_status", "never_run"),
        run_count=int(d.get("run_count") or 0),
    )


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    path = _store_file()
    _AUTOMATIONS.clear()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw.get("automations", []):
                record = _record_from_dict(item)
                _AUTOMATIONS[record.automation_id] = record
        except (OSError, ValueError, KeyError, TypeError):
            _AUTOMATIONS.clear()
    _LOADED = True


def _save() -> None:
    path = _store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "automations": [_record_to_dict(a) for a in sorted(_AUTOMATIONS.values(), key=lambda x: x.created_at)]}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def create_automation(*, owner: str, template_id: str, display_name: str, inputs: dict | None = None,
                      interval_sec: int = 86400, next_run_at: float | None = None,
                      actor_type: str = "user", agent_id: str = "") -> AutomationSchedule:
    _load()
    now = time.time()
    record = AutomationSchedule(
        automation_id="auto_" + uuid.uuid4().hex[:16],
        owner=owner,
        template_id=template_id,
        actor_type=actor_type,
        agent_id=agent_id,
        display_name=display_name or template_id,
        inputs=dict(inputs or {}),
        interval_sec=max(60, int(interval_sec or 86400)),
        created_at=now,
        updated_at=now,
        next_run_at=float(next_run_at if next_run_at is not None else now + max(60, int(interval_sec or 86400))),
    )
    _AUTOMATIONS[record.automation_id] = record
    _save()
    return record


def list_automations(owner: str | None = None, include_inactive: bool = True) -> list[AutomationSchedule]:
    _load()
    records = list(_AUTOMATIONS.values())
    if owner:
        records = [a for a in records if a.owner == owner]
    if not include_inactive:
        records = [a for a in records if a.status == "active"]
    records.sort(key=lambda a: a.created_at, reverse=True)
    return records


def get_automation(automation_id: str) -> AutomationSchedule | None:
    _load()
    return _AUTOMATIONS.get(automation_id)


def update_automation(automation_id: str, **changes) -> AutomationSchedule | None:
    _load()
    existing = _AUTOMATIONS.get(automation_id)
    if not existing:
        return None
    changes.setdefault("updated_at", time.time())
    updated = replace(existing, **changes)
    _AUTOMATIONS[automation_id] = updated
    _save()
    return updated


def delete_automation(automation_id: str) -> bool:
    _load()
    if automation_id not in _AUTOMATIONS:
        return False
    del _AUTOMATIONS[automation_id]
    _save()
    return True


def due_automations(now: float | None = None, owner: str | None = None) -> list[AutomationSchedule]:
    when = time.time() if now is None else float(now)
    return [a for a in list_automations(owner=owner, include_inactive=False) if a.next_run_at <= when]


def mark_run(automation_id: str, *, run_id: str, status: str, now: float | None = None) -> AutomationSchedule | None:
    current = get_automation(automation_id)
    if not current:
        return None
    when = time.time() if now is None else float(now)
    return update_automation(
        automation_id,
        last_run_at=when,
        last_run_id=run_id,
        last_status=status,
        run_count=current.run_count + 1,
        next_run_at=when + current.interval_sec,
    )


def reload_for_tests() -> None:
    global _LOADED
    _LOADED = False
    _AUTOMATIONS.clear()
