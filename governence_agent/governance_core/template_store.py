"""Durable reusable template registry."""
from __future__ import annotations

import json
import os
import re
import store_concurrency
import time
import uuid
from dataclasses import replace
from pathlib import Path

from policy.manifest import INTERNAL
from template_models import TemplateRecord, TemplateVersion

_TEMPLATES: dict[str, TemplateRecord] = {}
_LOADED = False
_VALID_TYPES = {"excel", "powerpoint", "word", "email", "calendar", "workflow", "prompt", "generic"}


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
    configured = os.getenv("GOVERNANCE_TEMPLATE_STORE_FILE")
    if configured:
        return Path(configured).resolve()
    return _state_root() / "templates.json"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", (value or "template").strip().lower()).strip("_")
    return slug or "template"


def _version_to_dict(v: TemplateVersion) -> dict:
    return {
        "version": v.version,
        "created_by": v.created_by,
        "created_at": v.created_at,
        "content": dict(v.content or {}),
        "notes": v.notes,
    }


def _version_from_dict(d: dict) -> TemplateVersion:
    return TemplateVersion(
        version=int(d.get("version") or 1),
        created_by=d.get("created_by", ""),
        created_at=float(d.get("created_at") or 0.0),
        content=dict(d.get("content") or {}),
        notes=d.get("notes", ""),
    )


def _record_to_dict(r: TemplateRecord) -> dict:
    return {
        "template_id": r.template_id,
        "display_name": r.display_name,
        "template_type": r.template_type,
        "status": r.status,
        "owner": r.owner,
        "description": r.description,
        "classification": list(r.classification),
        "tags": list(r.tags),
        "allowed_workflow_ids": list(r.allowed_workflow_ids),
        "current_version": r.current_version,
        "versions": [_version_to_dict(v) for v in r.versions],
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }


def _record_from_dict(d: dict) -> TemplateRecord:
    return TemplateRecord(
        template_id=d["template_id"],
        display_name=d.get("display_name", ""),
        template_type=d.get("template_type", "generic"),
        status=d.get("status", "active"),
        owner=d.get("owner", ""),
        description=d.get("description", ""),
        classification=list(d.get("classification") or [INTERNAL]),
        tags=list(d.get("tags") or []),
        allowed_workflow_ids=list(d.get("allowed_workflow_ids") or []),
        current_version=int(d.get("current_version") or 1),
        versions=[_version_from_dict(v) for v in d.get("versions", [])],
        created_at=float(d.get("created_at") or 0.0),
        updated_at=float(d.get("updated_at") or 0.0),
    )


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    path = _store_file()
    _TEMPLATES.clear()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw.get("templates", []):
                record = _record_from_dict(item)
                _TEMPLATES[record.template_id] = record
        except (OSError, ValueError, KeyError, TypeError):
            _TEMPLATES.clear()
    _LOADED = True


def _save() -> None:
    path = _store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "templates": [_record_to_dict(r) for r in sorted(_TEMPLATES.values(), key=lambda x: x.created_at)]}
    store_concurrency.atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))


def _validate(template_type: str, content: dict) -> None:
    if template_type not in _VALID_TYPES:
        raise ValueError(f"unsupported template_type {template_type!r}")
    if not isinstance(content, dict):
        raise ValueError("content must be an object")
    if template_type == "workflow":
        for key in ("goal", "inputs", "steps", "outputs"):
            if key not in content:
                raise ValueError(f"workflow template content requires {key!r}")
    if template_type in {"excel", "powerpoint", "word"} and not (content.get("sections") or content.get("tables") or content.get("layout")):
        raise ValueError(f"{template_type} template content requires sections, tables, or layout")


def create_template(*, display_name: str, template_type: str, content: dict, created_by: str,
                    description: str = "", classification: list[str] | None = None,
                    tags: list[str] | None = None, allowed_workflow_ids: list[str] | None = None,
                    notes: str = "", template_id: str = "") -> TemplateRecord:
    _load()
    template_type = (template_type or "generic").strip().lower()
    _validate(template_type, content)
    now = time.time()
    base = _slug(template_id or display_name or template_type)
    tid = base
    while tid in _TEMPLATES:
        tid = f"{base}_{uuid.uuid4().hex[:6]}"
    version = TemplateVersion(version=1, created_by=created_by, created_at=now, content=dict(content), notes=notes)
    record = TemplateRecord(
        template_id=tid,
        display_name=display_name or tid,
        template_type=template_type,
        status="active",
        owner=created_by,
        description=description,
        classification=sorted(set(classification or [INTERNAL])),
        tags=sorted(set(tags or [])),
        allowed_workflow_ids=sorted(set(allowed_workflow_ids or [])),
        current_version=1,
        versions=[version],
        created_at=now,
        updated_at=now,
    )
    _TEMPLATES[tid] = record
    _save()
    return record


def list_templates(*, status: str | None = None, template_type: str | None = None, include_disabled: bool = False) -> list[TemplateRecord]:
    _load()
    records = list(_TEMPLATES.values())
    if not include_disabled:
        records = [r for r in records if r.status != "disabled"]
    if status:
        records = [r for r in records if r.status == status]
    if template_type:
        records = [r for r in records if r.template_type == template_type]
    records.sort(key=lambda r: (r.template_type, r.display_name.lower()))
    return records


def get_template(template_id: str) -> TemplateRecord | None:
    _load()
    return _TEMPLATES.get(template_id)


def add_version(template_id: str, *, content: dict, created_by: str, notes: str = "") -> TemplateRecord | None:
    _load()
    existing = _TEMPLATES.get(template_id)
    if not existing:
        return None
    _validate(existing.template_type, content)
    next_version = max([v.version for v in existing.versions] or [0]) + 1
    version = TemplateVersion(version=next_version, created_by=created_by, created_at=time.time(), content=dict(content), notes=notes)
    updated = replace(existing, versions=[*existing.versions, version], current_version=next_version, updated_at=time.time())
    _TEMPLATES[template_id] = updated
    _save()
    return updated


def update_template(template_id: str, **changes) -> TemplateRecord | None:
    _load()
    existing = _TEMPLATES.get(template_id)
    if not existing:
        return None
    allowed = {"display_name", "description", "classification", "tags", "allowed_workflow_ids", "status"}
    clean = {k: v for k, v in changes.items() if k in allowed}
    if "status" in clean and clean["status"] not in {"active", "draft", "disabled"}:
        raise ValueError("status must be active, draft, or disabled")
    if "classification" in clean:
        clean["classification"] = sorted(set(clean["classification"] or [INTERNAL]))
    if "tags" in clean:
        clean["tags"] = sorted(set(clean["tags"] or []))
    if "allowed_workflow_ids" in clean:
        clean["allowed_workflow_ids"] = sorted(set(clean["allowed_workflow_ids"] or []))
    clean["updated_at"] = time.time()
    updated = replace(existing, **clean)
    _TEMPLATES[template_id] = updated
    _save()
    return updated


def disable_template(template_id: str) -> TemplateRecord | None:
    return update_template(template_id, status="disabled")


def reload_for_tests() -> None:
    global _LOADED
    _LOADED = False
    _TEMPLATES.clear()
