"""Local artifact store used by the first office-assistant scaffold.

This is intentionally simple and filesystem-backed. It gives generated files a
stable id, metadata, checksums, and owner scoping. A Blob/DocSpace-backed store
can replace this module later while keeping the public functions intact.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from dataclasses import replace

from artifact_models import ArtifactRecord, ArtifactVersionRecord


def _retention_days_for(classification: list[str] | None) -> int:
    labels = {str(c).upper() for c in list(classification or [])}
    if "CONFIDENTIAL" in labels:
        return 30
    if "SENSITIVE" in labels:
        return 60
    if "PII" in labels:
        return 90
    return 180


def _root() -> Path:
    configured = os.getenv("GOVERNANCE_ARTIFACT_DIR")
    if configured:
        return Path(configured).resolve()
    if Path("/home/data").exists():
        return Path("/home/data/governance-artifacts").resolve()
    return Path("/tmp/governance-artifacts").resolve()


def _meta_path(artifact_dir: Path) -> Path:
    return artifact_dir / "metadata.json"


def _versions_path(artifact_dir: Path) -> Path:
    return artifact_dir / "versions.json"


def _record_from_dict(d: dict) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=d["artifact_id"],
        owner=d.get("owner", ""),
        title=d.get("title", ""),
        filename=d.get("filename", ""),
        type=d.get("type", ""),
        storage_path=d.get("storage_path", ""),
        mime_type=d.get("mime_type", "application/octet-stream"),
        classification=list(d.get("classification") or []),
        source_workflow_run_id=d.get("source_workflow_run_id", ""),
        source_tool_calls=list(d.get("source_tool_calls") or []),
        source_artifact_ids=list(d.get("source_artifact_ids") or []),
        checksum=d.get("checksum", ""),
        size_bytes=int(d.get("size_bytes") or 0),
        status=d.get("status", "ready"),
        created_at=float(d.get("created_at") or 0.0),
        expires_at=d.get("expires_at"),
        retention_days=d.get("retention_days"),
        current_version=int(d.get("current_version") or 1),
        version_count=int(d.get("version_count") or 1),
        latest_version_id=d.get("latest_version_id", "v1"),
    )


def _record_to_dict(r: ArtifactRecord) -> dict:
    return {
        "artifact_id": r.artifact_id,
        "owner": r.owner,
        "title": r.title,
        "filename": r.filename,
        "type": r.type,
        "storage_path": r.storage_path,
        "mime_type": r.mime_type,
        "classification": list(r.classification),
        "source_workflow_run_id": r.source_workflow_run_id,
        "source_tool_calls": list(r.source_tool_calls),
        "source_artifact_ids": list(r.source_artifact_ids),
        "checksum": r.checksum,
        "size_bytes": r.size_bytes,
        "status": r.status,
        "created_at": r.created_at,
        "expires_at": r.expires_at,
        "retention_days": r.retention_days,
        "current_version": r.current_version,
        "version_count": r.version_count,
        "latest_version_id": r.latest_version_id,
    }


def _version_to_dict(v: ArtifactVersionRecord) -> dict:
    return {
        "artifact_id": v.artifact_id,
        "version_id": v.version_id,
        "version_number": v.version_number,
        "filename": v.filename,
        "storage_path": v.storage_path,
        "mime_type": v.mime_type,
        "checksum": v.checksum,
        "size_bytes": v.size_bytes,
        "created_by": v.created_by,
        "created_at": v.created_at,
        "note": v.note,
    }


def _version_from_dict(d: dict) -> ArtifactVersionRecord:
    return ArtifactVersionRecord(
        artifact_id=d.get("artifact_id", ""),
        version_id=d.get("version_id", "v1"),
        version_number=int(d.get("version_number") or 1),
        filename=d.get("filename", ""),
        storage_path=d.get("storage_path", ""),
        mime_type=d.get("mime_type", "application/octet-stream"),
        checksum=d.get("checksum", ""),
        size_bytes=int(d.get("size_bytes") or 0),
        created_by=d.get("created_by", ""),
        created_at=float(d.get("created_at") or 0.0),
        note=d.get("note", ""),
    )


def _load_versions(record: ArtifactRecord) -> list[ArtifactVersionRecord]:
    artifact_dir = _artifact_dir_for(record)
    path = _versions_path(artifact_dir)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            versions = [_version_from_dict(item) for item in raw.get("versions", [])]
            if versions:
                versions.sort(key=lambda v: v.version_number)
                return versions
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return [ArtifactVersionRecord(
        artifact_id=record.artifact_id,
        version_id="v1",
        version_number=1,
        filename=record.filename,
        storage_path=record.storage_path,
        mime_type=record.mime_type,
        checksum=record.checksum,
        size_bytes=record.size_bytes,
        created_by=record.owner,
        created_at=record.created_at,
        note="original",
    )]


def _save_versions(record: ArtifactRecord, versions: list[ArtifactVersionRecord]) -> None:
    artifact_dir = _artifact_dir_for(record)
    _versions_path(artifact_dir).write_text(json.dumps({
        "version": 1,
        "versions": [_version_to_dict(v) for v in sorted(versions, key=lambda x: x.version_number)],
    }, indent=2), encoding="utf-8")


def list_versions(artifact_id: str) -> list[ArtifactVersionRecord]:
    record = get_artifact(artifact_id)
    if record is None:
        return []
    return _load_versions(record)


def get_version(artifact_id: str, version_id: str) -> ArtifactVersionRecord | None:
    for version in list_versions(artifact_id):
        if version.version_id == version_id:
            return version
    return None


def create_version(*, artifact_id: str, payload: bytes, filename: str | None = None, mime_type: str | None = None, created_by: str = "", note: str = "") -> ArtifactVersionRecord | None:
    record = get_artifact(artifact_id)
    if record is None:
        return None
    artifact_dir = _artifact_dir_for(record)
    if not _is_within_root(artifact_dir):
        raise ValueError("refusing to version artifact outside artifact root")
    versions = _load_versions(record)
    next_number = max(v.version_number for v in versions) + 1
    version_id = f"v{next_number}"
    safe_name = Path(filename or record.filename).name or record.filename
    version_dir = artifact_dir / "versions" / version_id
    version_dir.mkdir(parents=True, exist_ok=False)
    file_path = version_dir / safe_name
    file_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    created = time.time()
    version = ArtifactVersionRecord(
        artifact_id=record.artifact_id,
        version_id=version_id,
        version_number=next_number,
        filename=safe_name,
        storage_path=str(file_path),
        mime_type=mime_type or record.mime_type,
        checksum=f"sha256:{digest}",
        size_bytes=len(payload),
        created_by=created_by or record.owner,
        created_at=created,
        note=note,
    )
    versions.append(version)
    _save_versions(record, versions)
    updated = replace(
        record,
        filename=safe_name,
        storage_path=str(file_path),
        mime_type=version.mime_type,
        checksum=version.checksum,
        size_bytes=version.size_bytes,
        current_version=next_number,
        version_count=len(versions),
        latest_version_id=version_id,
    )
    _meta_path(artifact_dir).write_text(json.dumps(_record_to_dict(updated), indent=2), encoding="utf-8")
    return version

def create_artifact(
    *,
    owner: str,
    title: str,
    filename: str,
    payload: bytes,
    artifact_type: str,
    mime_type: str,
    classification: list[str] | None = None,
    source_workflow_run_id: str = "",
    source_tool_calls: list[str] | None = None,
    source_artifact_ids: list[str] | None = None,
    retention_days: int | None = None,
) -> ArtifactRecord:
    root = _root()
    artifact_id = "art_" + uuid.uuid4().hex[:16]
    artifact_dir = root / time.strftime("%Y/%m") / artifact_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    safe_name = Path(filename).name or f"{artifact_id}.{artifact_type}"
    file_path = artifact_dir / safe_name
    file_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    labels = sorted(set(classification or ["INTERNAL"]))
    days = _retention_days_for(labels) if retention_days is None else max(0, int(retention_days))
    created = time.time()
    record = ArtifactRecord(
        artifact_id=artifact_id,
        owner=owner,
        title=title or safe_name,
        filename=safe_name,
        type=artifact_type,
        storage_path=str(file_path),
        mime_type=mime_type,
        classification=labels,
        source_workflow_run_id=source_workflow_run_id,
        source_tool_calls=list(source_tool_calls or []),
        source_artifact_ids=list(source_artifact_ids or []),
        checksum=f"sha256:{digest}",
        size_bytes=len(payload),
        created_at=created,
        expires_at=created + days * 86400,
        retention_days=days,
        current_version=1,
        version_count=1,
        latest_version_id="v1",
    )
    _meta_path(artifact_dir).write_text(json.dumps(_record_to_dict(record), indent=2), encoding="utf-8")
    _versions_path(artifact_dir).write_text(json.dumps({
        "version": 1,
        "versions": [_version_to_dict(ArtifactVersionRecord(
            artifact_id=artifact_id,
            version_id="v1",
            version_number=1,
            filename=safe_name,
            storage_path=str(file_path),
            mime_type=mime_type,
            checksum=f"sha256:{digest}",
            size_bytes=len(payload),
            created_by=owner,
            created_at=created,
            note="original",
        ))],
    }, indent=2), encoding="utf-8")
    return record


def get_artifact(artifact_id: str) -> ArtifactRecord | None:
    if not artifact_id:
        return None
    root = _root()
    for meta in root.glob(f"*/*/{artifact_id}/metadata.json"):
        try:
            return _record_from_dict(json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            return None
    return None


def list_artifacts(owner: str | None = None, limit: int = 100) -> list[ArtifactRecord]:
    records: list[ArtifactRecord] = []
    root = _root()
    if not root.exists():
        return []
    for meta in root.glob("*/*/art_*/metadata.json"):
        try:
            record = _record_from_dict(json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            continue
        if owner and record.owner != owner:
            continue
        records.append(record)
    records.sort(key=lambda r: r.created_at, reverse=True)
    return records[:limit]


def _artifact_dir_for(record: ArtifactRecord) -> Path:
    path = Path(record.storage_path).resolve()
    parent = path.parent
    if parent.parent.name == "versions":
        return parent.parent.parent
    return parent


def _is_within_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(_root())
        return True
    except ValueError:
        return False


def delete_artifact(artifact_id: str, *, owner: str | None = None) -> bool:
    record = get_artifact(artifact_id)
    if record is None:
        return False
    if owner and record.owner != owner:
        return False
    artifact_dir = _artifact_dir_for(record)
    if not _is_within_root(artifact_dir):
        raise ValueError("refusing to delete artifact outside artifact root")
    if artifact_dir.exists():
        shutil.rmtree(artifact_dir)
    return True


def purge_expired(*, now: float | None = None, owner: str | None = None, limit: int = 1000) -> list[ArtifactRecord]:
    current = time.time() if now is None else now
    expired = [r for r in list_artifacts(owner=owner, limit=limit) if r.expires_at is not None and r.expires_at <= current]
    purged: list[ArtifactRecord] = []
    for record in expired:
        if delete_artifact(record.artifact_id, owner=owner):
            purged.append(record)
    return purged

