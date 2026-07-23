"""Durable document review sessions for governed artifacts.

This is the local scaffold for ONLYOFFICE/DocSpace style collaboration. The
provider/editor_url fields let a real editor connector plug in later while the
review lifecycle, comments, authorization hooks, and audit trail exist today.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import replace
from pathlib import Path

from document_review_models import DocumentReviewComment, DocumentReviewSession

_REVIEWS: dict[str, DocumentReviewSession] = {}
_LOADED = False
_VALID_STATUSES = {"open", "approved", "changes_requested", "closed"}


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
    configured = os.getenv("GOVERNANCE_DOCUMENT_REVIEW_STORE_FILE")
    if configured:
        return Path(configured).resolve()
    return _state_root() / "document-reviews.json"


def _comment_to_dict(c: DocumentReviewComment) -> dict:
    return {
        "comment_id": c.comment_id,
        "author": c.author,
        "body": c.body,
        "anchor": c.anchor,
        "created_at": c.created_at,
    }


def _comment_from_dict(d: dict) -> DocumentReviewComment:
    return DocumentReviewComment(
        comment_id=d.get("comment_id", ""),
        author=d.get("author", ""),
        body=d.get("body", ""),
        anchor=d.get("anchor", ""),
        created_at=float(d.get("created_at") or 0.0),
    )


def _review_to_dict(r: DocumentReviewSession) -> dict:
    return {
        "review_id": r.review_id,
        "artifact_id": r.artifact_id,
        "artifact_version_id": r.artifact_version_id,
        "requested_by": r.requested_by,
        "owner": r.owner,
        "status": r.status,
        "provider": r.provider,
        "editor_url": r.editor_url,
        "reason": r.reason,
        "decision": r.decision,
        "decided_by": r.decided_by,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "decided_at": r.decided_at,
        "comments": [_comment_to_dict(c) for c in r.comments],
    }


def _review_from_dict(d: dict) -> DocumentReviewSession:
    return DocumentReviewSession(
        review_id=d["review_id"],
        artifact_id=d.get("artifact_id", ""),
        artifact_version_id=d.get("artifact_version_id", ""),
        requested_by=d.get("requested_by", ""),
        owner=d.get("owner", ""),
        status=d.get("status", "open"),
        provider=d.get("provider", "local"),
        editor_url=d.get("editor_url", ""),
        reason=d.get("reason", ""),
        decision=d.get("decision", ""),
        decided_by=d.get("decided_by", ""),
        created_at=float(d.get("created_at") or 0.0),
        updated_at=float(d.get("updated_at") or 0.0),
        decided_at=d.get("decided_at"),
        comments=[_comment_from_dict(c) for c in d.get("comments", [])],
    )


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    path = _store_file()
    _REVIEWS.clear()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            for item in raw.get("reviews", []):
                record = _review_from_dict(item)
                _REVIEWS[record.review_id] = record
        except (OSError, ValueError, KeyError, TypeError):
            _REVIEWS.clear()
    _LOADED = True


def _save() -> None:
    path = _store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "reviews": [_review_to_dict(r) for r in sorted(_REVIEWS.values(), key=lambda x: x.created_at)]}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def reload_for_tests() -> None:
    global _LOADED
    _LOADED = False
    _REVIEWS.clear()


def create_review(*, artifact_id: str, artifact_version_id: str, requested_by: str, owner: str,
                  reason: str = "", provider: str = "local", editor_url: str = "") -> DocumentReviewSession:
    _load()
    now = time.time()
    review = DocumentReviewSession(
        review_id="rev_" + uuid.uuid4().hex[:16],
        artifact_id=artifact_id,
        artifact_version_id=artifact_version_id or "latest",
        requested_by=requested_by,
        owner=owner,
        status="open",
        provider=(provider or "local").strip() or "local",
        editor_url=editor_url.strip(),
        reason=reason.strip(),
        created_at=now,
        updated_at=now,
    )
    _REVIEWS[review.review_id] = review
    _save()
    return review


def list_reviews(*, artifact_id: str | None = None, owner: str | None = None, include_closed: bool = True) -> list[DocumentReviewSession]:
    _load()
    records = list(_REVIEWS.values())
    if artifact_id:
        records = [r for r in records if r.artifact_id == artifact_id]
    if owner:
        records = [r for r in records if r.owner == owner or r.requested_by == owner]
    if not include_closed:
        records = [r for r in records if r.status == "open"]
    records.sort(key=lambda r: r.updated_at, reverse=True)
    return records


def get_review(review_id: str) -> DocumentReviewSession | None:
    _load()
    return _REVIEWS.get(review_id)


def add_comment(review_id: str, *, author: str, body: str, anchor: str = "") -> DocumentReviewSession | None:
    _load()
    existing = _REVIEWS.get(review_id)
    if existing is None:
        return None
    body = body.strip()
    if not body:
        raise ValueError("comment body is required")
    now = time.time()
    comment = DocumentReviewComment(
        comment_id="cmt_" + uuid.uuid4().hex[:12],
        author=author,
        body=body,
        anchor=anchor.strip(),
        created_at=now,
    )
    updated = replace(existing, comments=[*existing.comments, comment], updated_at=now)
    _REVIEWS[review_id] = updated
    _save()
    return updated


def decide_review(review_id: str, *, status: str, decided_by: str, decision: str = "") -> DocumentReviewSession | None:
    _load()
    existing = _REVIEWS.get(review_id)
    if existing is None:
        return None
    if status not in _VALID_STATUSES or status == "open":
        raise ValueError("status must be approved, changes_requested, or closed")
    now = time.time()
    updated = replace(
        existing,
        status=status,
        decision=decision.strip(),
        decided_by=decided_by,
        decided_at=now,
        updated_at=now,
    )
    _REVIEWS[review_id] = updated
    _save()
    return updated