"""Azure OpenAI embedding client for personal-KB semantic search.

Hosted, pay-per-call embeddings (text-embedding-3-large) on a dedicated Azure
OpenAI resource -- deliberately NOT the self-hosted chat model's endpoint, and
NOT AraTest's shared AzureOpenAIService-LanguageModels resource. See
digest_persoanl_kb.md §0.1 for why: this app owns every resource it depends on,
same as its dedicated Cosmos account and chat-model VM.

Lazy-imports openai.AzureOpenAI so a process that never configures embeddings
(e.g. mcp-knowledge running with only the company-tier local fallback) pays no
import cost. Every function returns None/"" on any failure (missing config,
network/API error) rather than raising -- callers degrade to TF-IDF scoring,
matching company-tier search_knowledge's own direct/proxy/local fallback chain.
"""
from __future__ import annotations

import os

# text-embedding-3-large natively returns 3072-dim vectors. The API supports
# requesting a smaller size via `dimensions` (Matryoshka-trained, so shorter
# vectors stay meaningful) -- this keeps each chunk's stored vector (and the
# in-app cosine-similarity cost of scoring a whole owner partition per query)
# smaller without switching models. Override via env if you want the full size.
_DEFAULT_DIMENSIONS = 1024


def configured() -> bool:
    return bool(os.getenv("GOVERNANCE_EMBEDDING_ENDPOINT") and os.getenv("GOVERNANCE_EMBEDDING_API_KEY"))


def _client():
    from openai import AzureOpenAI

    # .rstrip("/"): the openai SDK has a known bug where a trailing slash on
    # azure_endpoint produces a double-slash URL and a 404
    # (github.com/openai/openai-python/issues/1894) -- every endpoint shown in
    # the Azure portal includes the trailing slash, so strip it here rather
    # than relying on whoever pastes the env var to remember not to.
    return AzureOpenAI(
        api_key=os.getenv("GOVERNANCE_EMBEDDING_API_KEY"),
        azure_endpoint=(os.getenv("GOVERNANCE_EMBEDDING_ENDPOINT") or "").rstrip("/"),
        api_version=os.getenv("GOVERNANCE_EMBEDDING_API_VERSION") or "2024-12-01-preview",
    )


def _deployment() -> str:
    return os.getenv("GOVERNANCE_EMBEDDING_DEPLOYMENT") or "text-embedding-3-large"


def dimensions() -> int:
    return int(os.getenv("GOVERNANCE_EMBEDDING_DIMENSIONS") or _DEFAULT_DIMENSIONS)


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch of texts in one call. None on any failure or missing config
    -- callers fall back to TF-IDF for that request rather than hard-failing."""
    if not texts or not configured():
        return None
    try:
        response = _client().embeddings.create(input=texts, model=_deployment(), dimensions=dimensions())
        return [item.embedding for item in response.data]
    except Exception:
        return None


def embed_query(text: str) -> list[float] | None:
    if not text or not text.strip():
        return None
    result = embed_texts([text])
    return result[0] if result else None
