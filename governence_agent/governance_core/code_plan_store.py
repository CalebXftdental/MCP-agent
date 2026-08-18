"""Durable store for read-only opencode/code-planning requests."""
from __future__ import annotations

import json
import os
import re
import store_concurrency
import time
import uuid
from dataclasses import replace
from pathlib import Path

from code_plan_models import CodePlanRecord

_PLANS: dict[str, CodePlanRecord] = {}
_LOADED = False
_VALID_TYPES = {"change_plan", "repo_review", "template_generation"}
_VALID_RISKS = {"low", "medium", "high"}


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
    configured = os.getenv("GOVERNANCE_CODE_PLAN_STORE_FILE")
    if configured:
        return Path(configured).resolve()
    return _state_root() / "code_plans.json"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", (value or "code_plan").strip().lower()).strip("_")
    return slug or "code_plan"


def _record_to_dict(r: CodePlanRecord) -> dict:
    return {
        "plan_id": r.plan_id,
        "owner": r.owner,
        "request": r.request,
        "plan_type": r.plan_type,
        "status": r.status,
        "risk_level": r.risk_level,
        "summary": r.summary,
        "proposed_files": list(r.proposed_files),
        "proposed_commands": list(r.proposed_commands),
        "findings": list(r.findings),
        "template_draft": dict(r.template_draft or {}),
        "approval_id": r.approval_id,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }


def _record_from_dict(d: dict) -> CodePlanRecord:
    return CodePlanRecord(
        plan_id=d["plan_id"],
        owner=d.get("owner", ""),
        request=d.get("request", ""),
        plan_type=d.get("plan_type", "change_plan"),
        status=d.get("status", "planned"),
        risk_level=d.get("risk_level", "medium"),
        summary=d.get("summary", ""),
        proposed_files=list(d.get("proposed_files") or []),
        proposed_commands=list(d.get("proposed_commands") or []),
        findings=list(d.get("findings") or []),
        template_draft=dict(d.get("template_draft") or {}),
        approval_id=d.get("approval_id", ""),
        created_at=float(d.get("created_at") or 0.0),
        updated_at=float(d.get("updated_at") or 0.0),
    )


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    path = _store_file()
    _PLANS.clear()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw.get("plans", []):
                record = _record_from_dict(item)
                _PLANS[record.plan_id] = record
        except (OSError, ValueError, KeyError, TypeError):
            _PLANS.clear()
    _LOADED = True


def _save() -> None:
    path = _store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "plans": [_record_to_dict(r) for r in sorted(_PLANS.values(), key=lambda x: x.created_at)]}
    store_concurrency.atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))


def create_plan(*, owner: str, request: str, plan_type: str, risk_level: str = "medium", summary: str,
                proposed_files: list[str] | None = None, proposed_commands: list[str] | None = None,
                findings: list[str] | None = None, template_draft: dict | None = None,
                approval_id: str = "", plan_id: str = "") -> CodePlanRecord:
    _load()
    plan_type = (plan_type or "change_plan").strip().lower()
    risk_level = (risk_level or "medium").strip().lower()
    if plan_type not in _VALID_TYPES:
        raise ValueError(f"unsupported plan_type {plan_type!r}")
    if risk_level not in _VALID_RISKS:
        raise ValueError("risk_level must be low, medium, or high")
    now = time.time()
    base = _slug(plan_id or f"{plan_type}_{uuid.uuid4().hex[:8]}")
    pid = base
    while pid in _PLANS:
        pid = f"{base}_{uuid.uuid4().hex[:6]}"
    record = CodePlanRecord(
        plan_id=pid,
        owner=owner,
        request=request,
        plan_type=plan_type,
        status="planned",
        risk_level=risk_level,
        summary=summary,
        proposed_files=list(proposed_files or []),
        proposed_commands=list(proposed_commands or []),
        findings=list(findings or []),
        template_draft=dict(template_draft or {}),
        approval_id=approval_id,
        created_at=now,
        updated_at=now,
    )
    _PLANS[pid] = record
    _save()
    return record


def list_plans(*, owner: str | None = None, limit: int = 100) -> list[CodePlanRecord]:
    _load()
    records = list(_PLANS.values())
    if owner:
        records = [r for r in records if r.owner == owner]
    records.sort(key=lambda r: r.created_at, reverse=True)
    return records[:max(1, min(int(limit or 100), 500))]


def get_plan(plan_id: str) -> CodePlanRecord | None:
    _load()
    return _PLANS.get(plan_id)


def attach_approval(plan_id: str, approval_id: str) -> CodePlanRecord | None:
    _load()
    existing = _PLANS.get(plan_id)
    if not existing:
        return None
    updated = replace(existing, approval_id=approval_id, updated_at=time.time())
    _PLANS[plan_id] = updated
    _save()
    return updated


def reload_for_tests() -> None:
    global _LOADED
    _LOADED = False
    _PLANS.clear()
