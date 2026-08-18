"""mcp-knowledge -- read-only governed knowledge search/Q&A backend.

Deliberately read-only: no ingest tools exist here. Three tiers, tried in order,
first one configured wins:
  1. Direct Azure AI Search (AZURE_SEARCH_SERVICE_ENDPOINT/API_KEY/INDEX_NAME) --
     queries the SAME index AraTestEnvBE's ragAgent ingests into, via the
     azure-search-documents SDK directly. Read-only BY CODE (only .search() is
     ever called, never upload_documents/delete_documents/merge_documents) --
     note this is enforced by what this module calls, not by the API key's own
     scope: the configured key is currently AraTestEnvBE's shared admin key
     (it has to be, since AraTestEnvBE's own ingestion code uses the same key to
     write). Provisioning a separate Azure AI Search QUERY key for this app
     specifically (Azure supports multiple keys per service) would make
     "read-only" a property of the credential too, not just of this code --
     recommended as a follow-up, not done here (requires management-plane
     access this app doesn't have).
  2. HTTP proxy to AraTestEnvBE's ragAgent API (KNOWLEDGE_RETRIEVAL_BASE_URL),
     see /api/ai-search/test-retriever, /api/ai-search/chat -- kept for if/when
     that service is actually deployed somewhere reachable.
  3. A small local JSON-backed store (knowledge_store.py) for dev/test when
     neither remote is configured -- that local store's own ingest functions
     still exist for seeding test fixtures directly in Python, but nothing here
     exposes them as a callable tool.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import httpx
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.getenv("KNOWLEDGE_ENV_FILE") or ".env.local")

_CORE_DIR = str((Path(__file__).parent.parent / "governance_core").resolve())
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

import base64

import knowledge_store
import personal_knowledge_store
from policy import manifest


def _allowed_hosts() -> list[str]:
    raw = (os.getenv("KNOWLEDGE_ALLOWED_HOSTS") or "").strip()
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    for h in raw.split(","):
        h = h.strip()
        if h:
            hosts.extend((h, f"{h}:*"))
    return hosts


mcp = FastMCP(
    "frontier-mcp-knowledge",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts(),
        allowed_origins=["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"],
    ),
)


def _ok(payload: dict) -> str:
    return json.dumps({"source": "knowledge", "status": "success", **payload}, ensure_ascii=False)


def _base(name: str) -> str:
    return (os.getenv(name) or "").rstrip("/")


def _api_headers() -> dict[str, str]:
    key = os.getenv("KNOWLEDGE_API_KEY") or os.getenv("X_API_KEY") or ""
    return {"x-api-key": key} if key else {}


def _timeout() -> float:
    return float(os.getenv("KNOWLEDGE_HTTP_TIMEOUT_SEC") or "60")


def _result_items(raw: dict) -> list[dict]:
    items = raw.get("results") or raw.get("context_chunks") or []
    out = []
    for idx, item in enumerate(items):
        content = item.get("content") or item.get("text") or item.get("caption") or ""
        out.append({
            "chunkId": item.get("chunk_id") or item.get("id") or f"remote_{idx}",
            "documentId": item.get("filename") or item.get("documentId") or "remote",
            "documentTitle": item.get("filename") or item.get("documentTitle") or "Remote document",
            "filename": item.get("filename") or "",
            "ordinal": item.get("chunk_index") or idx,
            "text": content[:700],
            "score": item.get("score") or item.get("reranker_score") or 0,
            "classification": [manifest.INTERNAL],
            "page": item.get("page"),
            "url": item.get("url") or item.get("doc_link"),
        })
    return out


# ── Tier 1: direct Azure AI Search (same index AraTestEnvBE's ragAgent owns) ──

def _direct_search_client():
    """None if not configured. Built fresh per call (cheap) rather than cached at
    import time, so tests/dev can toggle env vars without restarting the process."""
    endpoint = _base("AZURE_SEARCH_SERVICE_ENDPOINT")
    key = os.getenv("AZURE_SEARCH_API_KEY") or ""
    index_name = os.getenv("AZURE_SEARCH_INDEX_NAME") or ""
    if not (endpoint and key and index_name):
        return None
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents import SearchClient
    return SearchClient(endpoint, index_name, AzureKeyCredential(key))


def _direct_result_items(docs: list[dict]) -> list[dict]:
    out = []
    for idx, d in enumerate(docs):
        content = d.get("content") or d.get("caption") or ""
        out.append({
            "chunkId": d.get("chunk_id") or d.get("id") or f"direct_{idx}",
            "documentId": d.get("filename") or "indexed",
            "documentTitle": d.get("filename") or "Indexed document",
            "filename": d.get("filename") or "",
            "ordinal": d.get("chunk_index") if d.get("chunk_index") is not None else idx,
            "text": content[:700],
            "score": d.get("@search.score") or d.get("score") or 0,
            "classification": [manifest.INTERNAL],
            "page": d.get("page"),
            "url": d.get("url"),
        })
    return out


async def _direct_search(query: str, limit: int) -> dict | None:
    client = _direct_search_client()
    if client is None:
        return None
    def _run() -> list[dict]:
        # SearchClient is sync (no writes -- .search() only); run off the event
        # loop so one slow query doesn't block other in-flight MCP calls.
        return [dict(d) for d in client.search(search_text=query, top=limit, include_total_count=True)]
    docs = await asyncio.to_thread(_run)
    return {"query": query, "results": _direct_result_items(docs), "mode": "direct_azure_search"}


async def _direct_answer(query: str, limit: int) -> dict | None:
    searched = await _direct_search(query, limit)
    if searched is None:
        return None
    matches = searched["results"]
    if not matches:
        return {"query": query, "answer": "No matching indexed document content was found.", "citations": [], "mode": "direct_azure_search"}
    lines = [
        f"{i}. {m['documentTitle']}" + (f" (page {m['page']})" if m.get("page") else "") + f": {m['text']}"
        for i, m in enumerate(matches, 1)
    ]
    return {
        "query": query,
        "answer": "Relevant indexed context:\n" + "\n".join(lines),
        "citations": [{"documentId": m["documentId"], "chunkId": m["chunkId"], "documentTitle": m["documentTitle"], "score": m.get("score", 0), "url": m.get("url")} for m in matches],
        "mode": "direct_azure_search",
    }


# ── Tier 2: HTTP proxy to AraTestEnvBE's ragAgent API ─────────────────────────

async def _remote_search(query: str, limit: int) -> dict | None:
    base = _base("KNOWLEDGE_RETRIEVAL_BASE_URL") or _base("RAG_RETRIEVAL_BASE_URL") or _base("RAG_API_BASE_URL")
    if not base:
        return None
    payload = {"query": query, "top_k": limit, "use_multiquery": False, "use_hybrid": True, "min_score": 0.0}
    async with httpx.AsyncClient(timeout=_timeout()) as client:
        resp = await client.post(f"{base}/api/ai-search/test-retriever", json=payload, headers=_api_headers())
        resp.raise_for_status()
        raw = resp.json()
    return {"query": query, "results": _result_items(raw), "remote": raw, "mode": "remote"}


async def _remote_answer(query: str, limit: int) -> dict | None:
    base = _base("KNOWLEDGE_RETRIEVAL_BASE_URL") or _base("RAG_RETRIEVAL_BASE_URL") or _base("RAG_API_BASE_URL")
    if not base:
        return None
    async with httpx.AsyncClient(timeout=_timeout()) as client:
        resp = await client.post(f"{base}/api/ai-search/chat", json={"query": query}, headers=_api_headers())
        resp.raise_for_status()
        raw = resp.json()
    answer = raw.get("response") or raw.get("answer") or ""
    chunks = _result_items(raw)
    return {
        "query": query,
        "answer": answer,
        "citations": [{"documentId": c["documentId"], "chunkId": c["chunkId"], "documentTitle": c["documentTitle"], "score": c.get("score", 0), "url": c.get("url")} for c in chunks[:limit]],
        "remote": raw,
        "mode": "remote",
    }


@mcp.tool()
async def search_knowledge(owner: str, query: str, limit: int = 5, document_id: str = "") -> str:
    """Search indexed knowledge chunks (direct Azure AI Search, then the
    AraTestEnvBE HTTP proxy, then a local fallback -- first configured wins)."""
    direct = await _direct_search(query, limit)
    if direct is not None:
        return _ok(direct)
    remote = await _remote_search(query, limit)
    if remote is not None:
        return _ok(remote)
    results = knowledge_store.search(owner, query, limit, document_id or None)
    return _ok({"query": query, "results": results, "mode": "local"})


@mcp.tool()
async def answer_from_knowledge(owner: str, query: str, limit: int = 5, document_id: str = "") -> str:
    """Return a citation-backed extractive answer from indexed documents (same
    direct/proxy/local precedence as search_knowledge)."""
    direct = await _direct_answer(query, limit)
    if direct is not None:
        return _ok(direct)
    remote = await _remote_answer(query, limit)
    if remote is not None:
        return _ok(remote)
    result = knowledge_store.answer(owner, query, limit, document_id or None)
    return _ok({"query": query, **result, "mode": "local"})


# ── Personal knowledge tier: private per-owner store, no admin bypass ─────────
# `owner` is a literal argument here (this process is localhost-only, the
# gateway is its sole client -- see DEPLOY.md) but the gateway's own tool
# wrappers (gateway/app.py) never forward an LLM/caller-supplied owner: they
# hardcode it from the authenticated session before calling here. Do not add a
# route that lets an owner value reach this file from anywhere else.

@mcp.tool()
async def ingest_my_document(owner: str, title: str, filename: str, content_base64: str, classification: str = "") -> str:
    """Upload a document into the caller's own private knowledge base (PDF, docx,
    pptx, xlsx, txt, md, csv, json, log). Not visible to anyone else, including
    admins."""
    try:
        payload = base64.b64decode(content_base64)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"source": "knowledge", "status": "error", "message": f"invalid content_base64: {exc}"})
    classes = [c.strip() for c in classification.split(",") if c.strip()] or None
    try:
        doc = personal_knowledge_store.ingest_document(owner, title, filename, payload, classes)
    except ValueError as exc:
        return json.dumps({"source": "knowledge", "status": "error", "message": str(exc)})
    return _ok({"document": doc.public_dict()})


@mcp.tool()
async def search_my_documents(owner: str, query: str, limit: int = 5, document_id: str = "") -> str:
    """Search the caller's own private document chunks (semantic search, falls
    back to keyword search automatically)."""
    results = personal_knowledge_store.search(owner, query, limit, document_id or None)
    return _ok({"query": query, "results": results, "mode": "personal"})


@mcp.tool()
async def answer_from_my_documents(owner: str, query: str, limit: int = 5, document_id: str = "") -> str:
    """Return a citation-backed extractive answer from the caller's own private
    documents."""
    result = personal_knowledge_store.answer(owner, query, limit, document_id or None)
    return _ok({"query": query, **result, "mode": "personal"})


@mcp.tool()
async def list_my_documents(owner: str, limit: int = 100) -> str:
    """List documents in the caller's own private knowledge base."""
    docs = personal_knowledge_store.list_documents(owner, limit)
    return _ok({"documents": [d.public_dict() for d in docs]})


@mcp.tool()
async def delete_my_document(owner: str, document_id: str) -> str:
    """Delete a document from the caller's own private knowledge base."""
    deleted = personal_knowledge_store.delete_document(document_id, owner)
    return _ok({"documentId": document_id, "deleted": deleted})


async def _health(_request):
    return JSONResponse({"ok": True, "service": "mcp-knowledge"})


app = mcp.streamable_http_app()
app.add_route("/health", _health)


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("KNOWLEDGE_PORT") or "8050")
    print(f"[mcp-knowledge] Starting on 0.0.0.0:{port} (path /mcp)")
    uvicorn.run(app, host="0.0.0.0", port=port)
