"""Read-only knowledge base search and answering."""
from __future__ import annotations

from starlette.responses import JSONResponse
from store import get_store
import audit
import mcp_clients
import json
import knowledge_store

from .deps import _effective_category_ids, _session, _unauthorized


def _knowledge_allowed(claims: dict) -> bool:
    if claims.get("role") == "admin":
        return True
    record = get_store().get_consumer(claims.get("sub", ""))
    return bool(record and "knowledge" in _effective_category_ids(get_store(), record))


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
