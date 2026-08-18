"""Durable local registry for autonomous agent profiles."""
from __future__ import annotations

import json
import os
import store_concurrency
import time
import uuid
from dataclasses import replace
from pathlib import Path

from agent_models import AgentProfile

_AGENTS: dict[str, AgentProfile] = {}
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
    configured = os.getenv("GOVERNANCE_AGENT_STORE_FILE")
    if configured:
        return Path(configured).resolve()
    return _state_root() / "agents.json"


def _record_to_dict(a: AgentProfile) -> dict:
    return {
        "agent_id": a.agent_id,
        "display_name": a.display_name,
        "consumer_id": a.consumer_id,
        "status": a.status,
        "description": a.description,
        "categories": list(a.categories),
        "allowed_template_ids": list(a.allowed_template_ids),
        "max_runs_per_day": a.max_runs_per_day,
        "created_by": a.created_by,
        "created_at": a.created_at,
        "updated_at": a.updated_at,
        "last_run_at": a.last_run_at,
        "last_run_id": a.last_run_id,
        "run_count": a.run_count,
    }


def _record_from_dict(d: dict) -> AgentProfile:
    return AgentProfile(
        agent_id=d["agent_id"],
        display_name=d.get("display_name", ""),
        consumer_id=d.get("consumer_id", ""),
        status=d.get("status", "active"),
        description=d.get("description", ""),
        categories=list(d.get("categories") or []),
        allowed_template_ids=list(d.get("allowed_template_ids") or []),
        max_runs_per_day=max(1, int(d.get("max_runs_per_day") or 24)),
        created_by=d.get("created_by", ""),
        created_at=float(d.get("created_at") or 0.0),
        updated_at=float(d.get("updated_at") or 0.0),
        last_run_at=d.get("last_run_at"),
        last_run_id=d.get("last_run_id", ""),
        run_count=int(d.get("run_count") or 0),
    )


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    path = _store_file()
    _AGENTS.clear()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw.get("agents", []):
                record = _record_from_dict(item)
                _AGENTS[record.agent_id] = record
        except (OSError, ValueError, KeyError, TypeError):
            _AGENTS.clear()
    _LOADED = True


def _save() -> None:
    path = _store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "agents": [_record_to_dict(a) for a in sorted(_AGENTS.values(), key=lambda x: x.created_at)]}
    store_concurrency.atomic_write_text(path, json.dumps(payload, indent=2))


def create_agent(*, display_name: str, consumer_id: str, categories: list[str] | None = None,
                 allowed_template_ids: list[str] | None = None, description: str = "",
                 created_by: str = "", max_runs_per_day: int = 24) -> AgentProfile:
    _load()
    now = time.time()
    record = AgentProfile(
        agent_id="agent_" + uuid.uuid4().hex[:16],
        display_name=display_name or consumer_id,
        consumer_id=consumer_id,
        description=description,
        categories=sorted(set(categories or [])),
        allowed_template_ids=sorted(set(allowed_template_ids or [])),
        max_runs_per_day=max(1, int(max_runs_per_day or 24)),
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    _AGENTS[record.agent_id] = record
    _save()
    return record


def list_agents(include_disabled: bool = True) -> list[AgentProfile]:
    _load()
    records = list(_AGENTS.values())
    if not include_disabled:
        records = [a for a in records if a.status == "active"]
    records.sort(key=lambda a: a.created_at, reverse=True)
    return records


def get_agent(agent_id: str) -> AgentProfile | None:
    _load()
    return _AGENTS.get(agent_id)


def get_agent_by_consumer(consumer_id: str) -> AgentProfile | None:
    _load()
    for agent in _AGENTS.values():
        if agent.consumer_id == consumer_id:
            return agent
    return None


def update_agent(agent_id: str, **changes) -> AgentProfile | None:
    _load()
    existing = _AGENTS.get(agent_id)
    if not existing:
        return None
    changes.setdefault("updated_at", time.time())
    updated = replace(existing, **changes)
    _AGENTS[agent_id] = updated
    _save()
    return updated


def disable_agent(agent_id: str) -> AgentProfile | None:
    return update_agent(agent_id, status="disabled")


def mark_run(agent_id: str, *, run_id: str, now: float | None = None) -> AgentProfile | None:
    current = get_agent(agent_id)
    if not current:
        return None
    when = time.time() if now is None else float(now)
    return update_agent(agent_id, last_run_at=when, last_run_id=run_id, run_count=current.run_count + 1)


def allowed_for_template(agent: AgentProfile, template_id: str) -> bool:
    return agent.status == "active" and template_id in set(agent.allowed_template_ids)


def reload_for_tests() -> None:
    global _LOADED
    _LOADED = False
    _AGENTS.clear()
