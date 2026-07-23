"""Durable sharing registry for artifact access grants."""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import replace
from pathlib import Path

from artifact_share_models import ArtifactShareRecord

_SHARES: dict[str, ArtifactShareRecord] = {}
_LOADED = False

_ALLOWED_PERMISSIONS = {"view", "download"}


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
    configured = os.getenv("GOVERNANCE_ARTIFACT_SHARE_STORE_FILE")
    if configured:
        return Path(configured).resolve()
    return _state_root() / "artifact-shares.json"


def _record_to_dict(r: ArtifactShareRecord) -> dict:
    return {
        "share_id": r.share_id,
        "artifact_id": r.artifact_id,
        "owner": r.owner,
        "shared_with": r.shared_with,
        "permissions": list(r.permissions),
        "created_by": r.created_by,
        "status": r.status,
        "created_at": r.created_at,
        "expires_at": r.expires_at,
        "revoked_at": r.revoked_at,
        "revoked_by": r.revoked_by,
    }


def _record_from_dict(d: dict) -> ArtifactShareRecord:
    return ArtifactShareRecord(
        share_id=d["share_id"],
        artifact_id=d.get("artifact_id", ""),
        owner=d.get("owner", ""),
        shared_with=d.get("shared_with", ""),
        permissions=[p for p in list(d.get("permissions") or []) if p in _ALLOWED_PERMISSIONS],
        created_by=d.get("created_by", ""),
        status=d.get("status", "active"),
        created_at=float(d.get("created_at") or 0.0),
        expires_at=d.get("expires_at"),
        revoked_at=d.get("revoked_at"),
        revoked_by=d.get("revoked_by", ""),
    )


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    path = _store_file()
    _SHARES.clear()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw.get("shares", []):
                record = _record_from_dict(item)
                _SHARES[record.share_id] = record
        except (OSError, ValueError, KeyError, TypeError):
            _SHARES.clear()
    _LOADED = True


def _save() -> None:
    path = _store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "shares": [_record_to_dict(r) for r in sorted(_SHARES.values(), key=lambda x: x.created_at)],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _normalize_permissions(permissions: list[str] | None) -> list[str]:
    clean = [str(p).strip().lower() for p in list(permissions or [])]
    filtered = [p for p in clean if p in _ALLOWED_PERMISSIONS]
    if "view" not in filtered:
        filtered.append("view")
    return sorted(set(filtered))


def create_share(*, artifact_id: str, owner: str, shared_with: str, permissions: list[str] | None = None,
                 created_by: str = "", expires_at: float | None = None) -> ArtifactShareRecord:
    _load()
    target = str(shared_with or "").strip()
    if not target:
        raise ValueError("shared_with is required")
    now = time.time()
    record = ArtifactShareRecord(
        share_id="ashr_" + uuid.uuid4().hex[:16],
        artifact_id=artifact_id,
        owner=owner,
        shared_with=target,
        permissions=_normalize_permissions(permissions),
        created_by=created_by or owner,
        created_at=now,
        expires_at=expires_at,
    )
    _SHARES[record.share_id] = record
    _save()
    return record


def list_shares(*, artifact_id: str | None = None, owner: str | None = None, shared_with: str | None = None,
                include_revoked: bool = False) -> list[ArtifactShareRecord]:
    _load()
    records = list(_SHARES.values())
    if artifact_id:
        records = [r for r in records if r.artifact_id == artifact_id]
    if owner:
        records = [r for r in records if r.owner == owner]
    if shared_with:
        records = [r for r in records if r.shared_with == shared_with]
    if not include_revoked:
        records = [r for r in records if r.status == "active"]
    records.sort(key=lambda r: r.created_at, reverse=True)
    return records


def get_share(share_id: str) -> ArtifactShareRecord | None:
    _load()
    return _SHARES.get(share_id)


def is_active(record: ArtifactShareRecord, *, now: float | None = None) -> bool:
    current = time.time() if now is None else now
    return record.status == "active" and (record.expires_at is None or record.expires_at > current)


def active_share(artifact_id: str, shared_with: str, permission: str = "view", *, now: float | None = None) -> ArtifactShareRecord | None:
    perm = str(permission or "view").strip().lower()
    for record in list_shares(artifact_id=artifact_id, shared_with=shared_with):
        if is_active(record, now=now) and (perm == "view" or perm in record.permissions):
            return record
    return None


def revoke_share(share_id: str, *, revoked_by: str = "") -> ArtifactShareRecord | None:
    _load()
    record = _SHARES.get(share_id)
    if record is None:
        return None
    if record.status == "revoked":
        return record
    updated = replace(record, status="revoked", revoked_by=revoked_by, revoked_at=time.time())
    _SHARES[share_id] = updated
    _save()
    return updated


def reload_for_tests() -> None:
    global _LOADED
    _LOADED = False
    _SHARES.clear()
