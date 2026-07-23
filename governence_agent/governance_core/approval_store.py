"""Durable approval queue for consequence-bearing workflow actions."""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import replace
from pathlib import Path

from approval_models import ApprovalRecord

_APPROVALS: dict[str, ApprovalRecord] = {}
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
    configured = os.getenv("GOVERNANCE_APPROVAL_STORE_FILE")
    if configured:
        return Path(configured).resolve()
    return _state_root() / "approvals.json"


def _record_to_dict(r: ApprovalRecord) -> dict:
    return {
        "approval_id": r.approval_id,
        "requested_by": r.requested_by,
        "reason": r.reason,
        "status": r.status,
        "risk_level": r.risk_level,
        "artifact_ids": list(r.artifact_ids),
        "workflow_run_id": r.workflow_run_id,
        "approver": r.approver,
        "decision_note": r.decision_note,
        "created_at": r.created_at,
        "decided_at": r.decided_at,
    }


def _record_from_dict(d: dict) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=d["approval_id"],
        requested_by=d.get("requested_by", ""),
        reason=d.get("reason", ""),
        status=d.get("status", "pending"),
        risk_level=d.get("risk_level", "medium"),
        artifact_ids=list(d.get("artifact_ids") or []),
        workflow_run_id=d.get("workflow_run_id", ""),
        approver=d.get("approver", ""),
        decision_note=d.get("decision_note", ""),
        created_at=float(d.get("created_at") or 0.0),
        decided_at=d.get("decided_at"),
    )


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    path = _store_file()
    _APPROVALS.clear()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw.get("approvals", []):
                record = _record_from_dict(item)
                _APPROVALS[record.approval_id] = record
        except (OSError, ValueError, KeyError, TypeError):
            _APPROVALS.clear()
    _LOADED = True


def _save() -> None:
    path = _store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "approvals": [_record_to_dict(r) for r in sorted(_APPROVALS.values(), key=lambda x: x.created_at)],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def create_approval(*, requested_by: str, reason: str, risk_level: str = "medium",
                    artifact_ids: list[str] | None = None, workflow_run_id: str = "") -> ApprovalRecord:
    _load()
    record = ApprovalRecord(
        approval_id="appr_" + uuid.uuid4().hex[:16],
        requested_by=requested_by,
        reason=reason,
        risk_level=risk_level,
        artifact_ids=list(artifact_ids or []),
        workflow_run_id=workflow_run_id,
        created_at=time.time(),
    )
    _APPROVALS[record.approval_id] = record
    _save()
    return record


def list_approvals(requested_by: str | None = None, include_decided: bool = True) -> list[ApprovalRecord]:
    _load()
    records = list(_APPROVALS.values())
    if requested_by:
        records = [r for r in records if r.requested_by == requested_by]
    if not include_decided:
        records = [r for r in records if r.status == "pending"]
    records.sort(key=lambda r: r.created_at, reverse=True)
    return records


def get_approval(approval_id: str) -> ApprovalRecord | None:
    _load()
    return _APPROVALS.get(approval_id)


def decide_approval(approval_id: str, *, approver: str, status: str, note: str = "") -> ApprovalRecord | None:
    _load()
    existing = _APPROVALS.get(approval_id)
    if not existing or status not in ("approved", "denied"):
        return None
    updated = replace(existing, status=status, approver=approver, decision_note=note, decided_at=time.time())
    _APPROVALS[approval_id] = updated
    _save()
    return updated


def reload_for_tests() -> None:
    """Clear process memory so smoke tests can prove records are file-backed."""
    global _LOADED
    _LOADED = False
    _APPROVALS.clear()
