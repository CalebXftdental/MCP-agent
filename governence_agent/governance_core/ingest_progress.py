"""Cross-process stage tracking for personal-knowledge document ingestion, so
the frontend can show "extracting -> chunking -> embedding -> indexing ->
done" instead of one opaque spinner (personal_knowledge_store.ingest_document
already goes through those exact stages -- PDF extraction alone can run up to
GOVERNANCE_DOCINTEL_TIMEOUT_SEC=60s against Azure Document Intelligence, so a
plain "uploading..." spinner is actively misleading for the slow case this
exists to explain).

File-backed, one small JSON file per job under GOVERNANCE_STATE_DIR, NOT an
in-memory dict like bulk_result_cache.py -- ingestion runs inside the separate
mcp-knowledge process (see mcp_clients.call), while the gateway process is what
serves the polling endpoint, so the two must share state across a process
boundary. Same "ONE INSTANCE ONLY, shared filesystem" scope as every other
local-file store in this codebase (store/__init__.py, personal_knowledge_store's
local fallback) -- move to Cosmos/Redis before scaling out.

One file per job (rather than one shared file, like bulk_result_cache's dict)
means no cross-job lock contention and no read-modify-write races: each job's
writes come from a single background task, sequentially.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import store_concurrency

# Order mirrors personal_knowledge_store.ingest_document's actual stages, so
# the frontend can compute "how far along" without guessing at percentages
# server-side. "error" is a terminal stage outside this happy-path order.
STAGE_ORDER = ["uploaded", "extracting", "chunking", "embedding", "indexing", "done"]
TERMINAL_STAGES = frozenset({"done", "error"})

# Old job files are litter, not history -- nothing reads a job after the
# frontend stops polling it (it stops on the first terminal stage it sees).
# Generous enough to survive a slow poll loop noticing "done" late.
_TTL_SEC = 30 * 60


def _dir() -> Path:
    state_dir = Path(os.getenv("GOVERNANCE_STATE_DIR") or ".governance-state")
    d = state_dir / "ingest_progress"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(job_id: str) -> Path:
    # job_id is server-generated (see knowledge.py's `_new_job_id`), never
    # client-supplied, so no path-traversal concern -- but guard anyway since
    # this turns directly into a filesystem path.
    safe = "".join(c for c in job_id if c.isalnum() or c in ("-", "_")) or "job"
    return _dir() / f"{safe}.json"


def start(job_id: str, owner: str, filename: str) -> None:
    _write(job_id, {
        "jobId": job_id, "owner": owner, "filename": filename,
        "stage": "uploaded", "message": "", "documentId": "",
        "startedAt": time.time(), "updatedAt": time.time(),
    })


def set_stage(job_id: str, stage: str, *, message: str = "", document_id: str = "") -> None:
    record = _read(job_id)
    if record is None:
        return
    record["stage"] = stage
    record["message"] = message
    if document_id:
        record["documentId"] = document_id
    record["updatedAt"] = time.time()
    _write(job_id, record)


def get(job_id: str, owner: str) -> dict | None:
    """Owner-scoped read -- a job belonging to a different owner is reported as
    missing, same as `personal_knowledge_store.get_document`'s owner check."""
    record = _read(job_id)
    if record is None or record.get("owner") != owner:
        return None
    if time.time() - record.get("startedAt", 0) > _TTL_SEC:
        try:
            _path(job_id).unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return record


def _read(job_id: str) -> dict | None:
    path = _path(job_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write(job_id: str, record: dict) -> None:
    store_concurrency.atomic_write_text(_path(job_id), json.dumps(record, ensure_ascii=False))
