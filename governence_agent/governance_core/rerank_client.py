"""bge-reranker-v2-m3 cross-encoder client -- second stage of the KB retrieval
funnel. See ../howtousereranker.md for the full deployment writeup, measured
latency, and the stale-content-ranking limitation (it ranks semantic
relevance, not recency/authority -- not handled here; see that doc's "Known
limitation to carry into the integration" before assuming reranking alone
resolves contradictory KB content).

Same fail-soft shape as embeddings_client.py/docintel_client.py: any failure
(missing config, network/timeout error, bad response) returns None so callers
fall back to their own pre-rerank order (embedding-cosine, TF-IDF, or whatever
upstream tier produced the candidates) rather than breaking KB search over a
reranker outage -- per howtousereranker.md §5's explicit instruction.

Synchronous (httpx.post, not an async client): every call site in this repo
that reaches this module (personal_knowledge_store.search,
knowledge_store.search, mcp-knowledge/app.py's direct/remote tiers) is itself
a plain sync function called from inside an async tool handler without being
offloaded to a thread -- same accepted blocking-I/O shape docintel_client.py
and embeddings_client.py already use here, not a new pattern.
"""
from __future__ import annotations

import os

_DEFAULT_BASE_URL = "http://20.120.220.8:8091"


def configured() -> bool:
    return bool(os.getenv("RERANKER_API_KEY"))


def _base_url() -> str:
    return (os.getenv("RERANKER_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _timeout() -> float:
    return float(os.getenv("RERANKER_TIMEOUT_SEC") or "15")


def rerank(query: str, documents: list[str], top_n: int) -> list[int] | None:
    """Indices into `documents`, best-first, length <= top_n. None on any
    failure or missing config -- caller must fall back to its own pre-rerank
    order rather than treating None as "no results"."""
    if not query or not documents or not configured():
        return None
    try:
        import httpx

        resp = httpx.post(
            f"{_base_url()}/v1/rerank",
            headers={"Authorization": f"Bearer {os.getenv('RERANKER_API_KEY')}"},
            json={"query": query, "documents": documents, "top_n": max(1, int(top_n or 1))},
            timeout=_timeout(),
        )
        resp.raise_for_status()
        return [r["index"] for r in resp.json()["results"]]
    except Exception:
        return None
