"""Read-only company knowledge base search/answering, plus the personal
knowledge tier (private per-owner upload/list/delete/search) below."""
from __future__ import annotations

from pathlib import Path

from starlette.responses import JSONResponse
from store import get_store
import asyncio
import audit
import base64
import ingest_progress
import mcp_clients
import json
import knowledge_store
import logging
import uuid

from .deps import _effective_category_ids, _session, _unauthorized

_log = logging.getLogger("gateway.knowledge")

# Personal-KB upload through the Knowledge page's "Upload document" button
# (the REST route below) is PDF-only for the current stage -- the other
# formats in knowledge_store.ALLOWED_UPLOAD_EXTENSIONS parse locally rather
# than through Document Intelligence and haven't gotten the same scrutiny yet.
# Deliberately a narrower gate than knowledge_store.ALLOWED_UPLOAD_EXTENSIONS
# itself (personal_knowledge_store.ingest_document still accepts the full
# set) -- that keeps the chat tool (knowledge_ingest_my_document) and
# deploy_health_check.py's synthetic .txt lifecycle check working unchanged.
# Widen by pointing this back at knowledge_store.ALLOWED_UPLOAD_EXTENSIONS.
_CURRENT_UPLOAD_EXTENSIONS = frozenset({"pdf"})

# PDF extraction alone can run up to GOVERNANCE_DOCINTEL_TIMEOUT_SEC (default
# 60s) against Azure Document Intelligence -- give the ingest call real
# headroom above that rather than the default GATEWAY_BACKEND_TIMEOUT_SEC
# (30s), which would cut off a slow-but-legitimate PDF before DI's own timeout
# ever gets a chance to fire. See mcp_clients.call's docstring.
_INGEST_CALL_TIMEOUT_SEC = 90.0

# Ingestion runs in a background task so the POST can return the job id
# immediately for the frontend to poll (see _knowledge_mine_progress) instead
# of holding the HTTP request open for however long extraction+embedding
# takes. Tasks are kept here only so they aren't garbage-collected mid-flight
# -- asyncio only holds a weak reference otherwise -- and dropped again via
# their own done-callback; nothing ever reads this set's contents.
_background_tasks: set[asyncio.Task] = set()


def _knowledge_allowed(claims: dict) -> bool:
    if claims.get("role") == "admin":
        return True
    record = get_store().get_consumer(claims.get("sub", ""))
    return bool(record and "knowledge" in _effective_category_ids(get_store(), record))


def _personal_knowledge_allowed(claims: dict) -> bool:
    """Deliberately NO admin bypass, unlike _knowledge_allowed above -- personal
    KB is private to its owner regardless of role (digest_persoanl_kb.md §0). An
    admin's own access to their OWN personal documents still requires holding
    this category, same as anyone else; it grants nothing about anyone else's."""
    record = get_store().get_consumer(claims.get("sub", ""))
    return bool(record and "personal_knowledge" in _effective_category_ids(get_store(), record))


async def _knowledge_documents(request):
    """Read-only: lists whatever local documents already exist (e.g. from before
    this backend was switched to read-only, or local dev/test seeding). There is
    no POST -- new documents are never added through this gateway; they go
    through AraTestEnvBE's own ingestion pipeline into the shared knowledge base."""
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if not _knowledge_allowed(claims):
        return JSONResponse({"error": "knowledge access required"}, status_code=403)
    owner = None if claims["role"] == "admin" and request.query_params.get("all") == "1" else claims["name"]
    docs = knowledge_store.list_documents(owner=owner)
    return JSONResponse({"documents": [d.public_dict() for d in docs]})


async def _knowledge_document_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if not _knowledge_allowed(claims):
        return JSONResponse({"error": "knowledge access required"}, status_code=403)
    doc = knowledge_store.get_document(request.path_params["did"])
    if doc is None or (claims["role"] != "admin" and doc.owner != claims["name"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    if request.method == "DELETE":
        knowledge_store.delete_document(doc.document_id, None if claims["role"] == "admin" else claims["name"])
        audit.log_policy_change(actor=claims["name"], action="delete_knowledge", target=doc.document_id, detail=doc.filename)
        return JSONResponse({"ok": True})
    return JSONResponse({"document": doc.public_dict()})


async def _knowledge_search(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if not _knowledge_allowed(claims):
        return JSONResponse({"error": "knowledge access required"}, status_code=403)
    body = await request.json()
    query = str(body.get("query") or "")
    limit = int(body.get("limit") or 5)
    document_id = str(body.get("document_id") or body.get("documentId") or "")
    try:
        raw = await mcp_clients.call("knowledge", "search_knowledge", {"owner": claims["name"], "query": query, "limit": limit, "document_id": document_id})
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="search_knowledge", target=document_id or "all", detail=query[:120])
    return JSONResponse(payload)


async def _knowledge_answer(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if not _knowledge_allowed(claims):
        return JSONResponse({"error": "knowledge access required"}, status_code=403)
    body = await request.json()
    query = str(body.get("query") or "")
    limit = int(body.get("limit") or 5)
    document_id = str(body.get("document_id") or body.get("documentId") or "")
    try:
        raw = await mcp_clients.call("knowledge", "answer_from_knowledge", {"owner": claims["name"], "query": query, "limit": limit, "document_id": document_id})
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="answer_knowledge", target=document_id or "all", detail=query[:120])
    return JSONResponse(payload)


# ── Personal knowledge tier: private per-owner, no admin bypass ──────────────
# Every call below derives `owner` from the session (`claims["name"]`) the same
# way the company-tier routes above do -- never a client-supplied value. This is
# a second, independent enforcement point from the chat/MCP-tool-calling path
# (gateway/app.py's knowledge_* wrappers): the frontend calls these REST routes
# directly, bypassing _govern the same way the company-tier routes above already
# do, so owner-derivation here matters just as much as it does there.

async def _knowledge_mine(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if not _personal_knowledge_allowed(claims):
        return JSONResponse({"error": "personal knowledge access required"}, status_code=403)

    if request.method == "GET":
        raw = await mcp_clients.call("knowledge", "list_my_documents", {"owner": claims["name"], "limit": 100})
        return JSONResponse(json.loads(raw))

    # POST: multipart file upload. Validated and queued here, then actually
    # ingested (extract -> chunk -> embed -> index) in a background task --
    # see ingest_progress and _knowledge_mine_progress below for how the
    # frontend follows along instead of blocking on this request.
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "file is required (multipart/form-data)"}, status_code=400)
    payload = await upload.read()
    if not payload:
        return JSONResponse({"error": "uploaded file is empty"}, status_code=400)
    filename = str(form.get("filename") or upload.filename or "document")
    title = str(form.get("title") or filename)

    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix not in _CURRENT_UPLOAD_EXTENSIONS:
        allowed = ", ".join(sorted(_CURRENT_UPLOAD_EXTENSIONS))
        return JSONResponse(
            {"error": f'Unsupported file type ".{suffix or "?"}" -- only {allowed} is supported right now.'},
            status_code=400,
        )

    owner = claims["name"]
    job_id = "ing_" + uuid.uuid4().hex[:20]
    ingest_progress.start(job_id, owner=owner, filename=filename)

    task = asyncio.create_task(_run_ingest(job_id, owner, title, filename, payload))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return JSONResponse({"jobId": job_id, "status": "processing"})


async def _run_ingest(job_id: str, owner: str, title: str, filename: str, payload: bytes) -> None:
    """The actual ingest call, off the request/response cycle. Every exit path
    MUST leave the job in a terminal `ingest_progress` stage ("done" or
    "error") -- a poller (see _knowledge_mine_progress) waits for exactly
    that, and this task's exceptions otherwise vanish into asyncio's default
    "Task exception was never retrieved" logging with no user-visible effect."""
    try:
        raw = await mcp_clients.call(
            "knowledge", "ingest_my_document",
            {
                "owner": owner, "title": title, "filename": filename,
                "content_base64": base64.b64encode(payload).decode("ascii"), "classification": "",
                "job_id": job_id,
            },
            timeout=_INGEST_CALL_TIMEOUT_SEC,
        )
        result = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 -- must still resolve the job
        _log.exception("personal knowledge ingest %s failed", job_id)
        ingest_progress.set_stage(job_id, "error", message=str(exc) or "ingest failed")
        return

    if result.get("status") == "error":
        # ingest_my_document already marked the job "error" itself (with the
        # same message) before returning this -- nothing left to do here.
        return

    doc_id = (result.get("document") or {}).get("documentId", "")
    ingest_progress.set_stage(job_id, "done", document_id=doc_id)
    audit.log_policy_change(actor=owner, action="ingest_personal_knowledge", target=doc_id, detail=filename)


async def _knowledge_mine_progress(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if not _personal_knowledge_allowed(claims):
        return JSONResponse({"error": "personal knowledge access required"}, status_code=403)
    job_id = request.path_params["job_id"]
    record = ingest_progress.get(job_id, owner=claims["name"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({
        "jobId": record["jobId"], "stage": record["stage"], "message": record.get("message", ""),
        "documentId": record.get("documentId", ""), "filename": record.get("filename", ""),
    })


async def _knowledge_mine_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if not _personal_knowledge_allowed(claims):
        return JSONResponse({"error": "personal knowledge access required"}, status_code=403)
    did = request.path_params["did"]
    raw = await mcp_clients.call("knowledge", "delete_my_document", {"owner": claims["name"], "document_id": did})
    result = json.loads(raw)
    audit.log_policy_change(actor=claims["name"], action="delete_personal_knowledge", target=did)
    return JSONResponse(result)


async def _knowledge_mine_search(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if not _personal_knowledge_allowed(claims):
        return JSONResponse({"error": "personal knowledge access required"}, status_code=403)
    body = await request.json()
    query = str(body.get("query") or "")
    limit = int(body.get("limit") or 5)
    document_id = str(body.get("document_id") or body.get("documentId") or "")
    raw = await mcp_clients.call("knowledge", "search_my_documents", {
        "owner": claims["name"], "query": query, "limit": limit, "document_id": document_id,
    })
    audit.log_policy_change(actor=claims["name"], action="search_personal_knowledge", target=document_id or "all", detail=query[:120])
    return JSONResponse(json.loads(raw))
