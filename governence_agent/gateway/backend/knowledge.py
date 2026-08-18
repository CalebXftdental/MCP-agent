"""Read-only company knowledge base search/answering, plus the personal
knowledge tier (private per-owner upload/list/delete/search) below."""
from __future__ import annotations

from starlette.responses import JSONResponse
from store import get_store
import audit
import base64
import mcp_clients
import json
import knowledge_store

from .deps import _effective_category_ids, _session, _unauthorized


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

    # POST: multipart file upload.
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "file is required (multipart/form-data)"}, status_code=400)
    payload = await upload.read()
    if not payload:
        return JSONResponse({"error": "uploaded file is empty"}, status_code=400)
    filename = str(form.get("filename") or upload.filename or "document")
    title = str(form.get("title") or filename)
    try:
        raw = await mcp_clients.call("knowledge", "ingest_my_document", {
            "owner": claims["name"], "title": title, "filename": filename,
            "content_base64": base64.b64encode(payload).decode("ascii"), "classification": "",
        })
        result = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)
    if result.get("status") == "error":
        return JSONResponse({"error": result.get("message", "ingest failed")}, status_code=400)
    doc_id = (result.get("document") or {}).get("documentId", "")
    audit.log_policy_change(actor=claims["name"], action="ingest_personal_knowledge", target=doc_id, detail=filename)
    return JSONResponse(result)


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
