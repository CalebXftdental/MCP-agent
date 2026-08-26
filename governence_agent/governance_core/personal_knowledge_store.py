"""Personal knowledge base -- per-owner document store, isolated from the shared
company knowledge base (knowledge_store.py). See digest_persoanl_kb.md for the
full design; the short version:

  - Cosmos DB (same account as store/cosmos.py's consumers/categories/chatSessions
    -- new containers, not a new account), falling back to a local JSON file when
    Cosmos isn't configured, same store-backend-selection shape as store/__init__.py.
  - Partitioned by `owner`, which callers MUST derive server-side only (see
    gateway/app.py's knowledge_* tool wrappers) -- never trust an MCP-caller- or
    client-supplied owner for this module.
  - Real embeddings (Azure OpenAI, embeddings_client.py) with in-app cosine
    similarity, not a Cosmos-native vector index (see digest_persoanl_kb.md §0 for
    why) -- and a TF-IDF fallback (same shape as knowledge_store.py's algorithm)
    when embeddings are unconfigured or a call fails, so search degrades instead
    of breaking.
  - No admin bypass anywhere in this module -- every function takes/requires an
    `owner` and never has an "all owners" mode. (The one exception,
    `delete_all_for_owner`, is a cascade-delete hook for consumer offboarding, not
    an admin-read path.)

Extraction/chunking is shared with the company tier: this module imports
knowledge_store.extract_text/_chunk_text/_terms rather than reimplementing them.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import store_concurrency
import time
import uuid
from pathlib import Path

import embeddings_client
import knowledge_store
import rerank_client
from knowledge_models import KnowledgeChunk, KnowledgeDocument
from policy.manifest import INTERNAL

# Personal KBs start small (digest_persoanl_kb.md) -- below this many total
# chunks for an owner, the whole corpus is already smaller than the
# reranker's own tested/latency-known candidate-pool size (howtousereranker.md
# §3's 30-50 doc range), so search() skips the cosine/TF-IDF narrowing stage
# entirely and sends every chunk straight to the cross-encoder: more accurate
# than narrowing first (a lexical/embedding miss can't hide a chunk from a
# reranker that reads full query+doc text jointly), and cheap enough at this
# size. Above the threshold, narrow to _RERANK_CANDIDATE_POOL first, same
# reason embedding retrieval exists at all -- a cross-encoder is too
# expensive to run over an entire large corpus.
_VECTORIZE_THRESHOLD_CHUNKS = int(os.getenv("GOVERNANCE_PERSONAL_KB_VECTORIZE_THRESHOLD", "40"))
_RERANK_CANDIDATE_POOL = int(os.getenv("GOVERNANCE_KB_RERANK_CANDIDATE_POOL", "30"))

# Reranking only matters when there's an actual narrowing decision to make --
# if the candidate pool is already no bigger than what the caller asked for
# (plus this margin), every candidate is going back regardless of order, so
# the ~350ms-1s reranker round trip (howtousereranker.md §3) buys nothing.
# Skip it and use the fallback (cosine/TF-IDF) order as-is. margin=0 means
# "skip only when there's truly nothing to narrow"; raise it to also skip
# reranking small edges (e.g. 6 candidates for a limit of 5).
_RERANK_SKIP_MARGIN = int(os.getenv("GOVERNANCE_KB_RERANK_SKIP_MARGIN", "0"))


def _now() -> float:
    return time.time()


# ── backend selection (mirrors store/__init__.py's Cosmos > local shape) ──────

def _cosmos_configured() -> bool:
    return bool(os.getenv("GOVERNANCE_COSMOS_CONNECTION_STRING") or os.getenv("GOVERNANCE_COSMOS_URL"))


_cosmos_cache: dict[str, object] = {}


def _cosmos_containers():
    """Lazily create/cache the two personal-KB containers in the SAME Cosmos
    account/database store/cosmos.py already uses -- not a new account."""
    if "docs" not in _cosmos_cache:
        from azure.cosmos import CosmosClient, PartitionKey

        conn = os.getenv("GOVERNANCE_COSMOS_CONNECTION_STRING")
        if conn:
            client = CosmosClient.from_connection_string(conn)
        else:
            client = CosmosClient(
                os.getenv("GOVERNANCE_COSMOS_URL"), credential=os.getenv("GOVERNANCE_COSMOS_KEY")
            )
        db = client.create_database_if_not_exists(os.getenv("GOVERNANCE_COSMOS_DATABASE", "governance"))
        _cosmos_cache["docs"] = db.create_container_if_not_exists(
            "personalKnowledgeDocuments", PartitionKey(path="/owner")
        )
        _cosmos_cache["chunks"] = db.create_container_if_not_exists(
            "personalKnowledgeChunks", PartitionKey(path="/owner")
        )
    return _cosmos_cache["docs"], _cosmos_cache["chunks"]


def _local_store_file() -> Path:
    explicit = os.getenv("GOVERNANCE_PERSONAL_KNOWLEDGE_STORE_FILE")
    if explicit:
        return Path(explicit)
    state_dir = Path(os.getenv("GOVERNANCE_STATE_DIR") or ".governance-state")
    return state_dir / "personal_knowledge.json"


def _local_read() -> dict:
    path = _local_store_file()
    if not path.exists():
        return {"documents": {}, "chunks": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"documents": {}, "chunks": {}}


def _local_write(data: dict) -> None:
    path = _local_store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    store_concurrency.atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), newline="")


# ── item <-> model conversion (shared shape between Cosmos and local JSON) ────

def _doc_to_item(doc: KnowledgeDocument) -> dict:
    return {
        "id": doc.document_id, "owner": doc.owner, "title": doc.title, "filename": doc.filename,
        "source_type": doc.source_type, "classification": list(doc.classification),
        "created_at": doc.created_at, "updated_at": doc.updated_at, "chunk_count": doc.chunk_count,
        "checksum": doc.checksum, "metadata": dict(doc.metadata or {}),
    }


def _doc_from_item(d: dict) -> KnowledgeDocument:
    return KnowledgeDocument(
        document_id=d["id"], owner=d["owner"], title=d.get("title", d.get("filename", "Document")),
        filename=d.get("filename", "document.txt"), source_type=d.get("source_type", "txt"),
        classification=list(d.get("classification") or [INTERNAL]), created_at=float(d.get("created_at") or 0),
        updated_at=float(d.get("updated_at") or d.get("created_at") or 0), chunk_count=int(d.get("chunk_count") or 0),
        checksum=d.get("checksum", ""), metadata=dict(d.get("metadata") or {}),
    )


def _chunk_to_item(chunk: KnowledgeChunk) -> dict:
    return {
        "id": chunk.chunk_id, "owner": chunk.owner, "document_id": chunk.document_id,
        "ordinal": chunk.ordinal, "text": chunk.text, "terms": dict(chunk.terms or {}),
        "embedding": list(chunk.embedding) if chunk.embedding else None,
    }


def _chunk_from_item(d: dict) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=d["id"], document_id=d["document_id"], owner=d["owner"], ordinal=int(d.get("ordinal") or 0),
        text=d.get("text", ""), terms=dict(d.get("terms") or {}), embedding=list(d["embedding"]) if d.get("embedding") else None,
    )


# ── ingest ──────────────────────────────────────────────────────────────────

# digest_persoanl_kb.md §4 flagged both caps as an open item at design time
# ("not yet decided -- needs concrete numbers before ingest ships"). 20MB is a
# conservative default, comfortably under Document Intelligence's own inline
# (non-blob-URL) request-body limits on any tier/api-version -- tune down via
# env if the actual DI resource's confirmed limit is tighter, or up once it's
# confirmed to have real headroom. The per-owner document cap is a simple,
# cheap bound on total embedding-API cost and Cosmos RU/item-count exposure
# from one runaway or malicious upload loop -- not tied to any storage limit.
_MAX_UPLOAD_BYTES = int(os.getenv("GOVERNANCE_PERSONAL_KB_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
_MAX_DOCS_PER_OWNER = int(os.getenv("GOVERNANCE_PERSONAL_KB_MAX_DOCS_PER_OWNER", "300"))


def ingest_document(
    owner: str, title: str, filename: str, payload: bytes,
    classification: list[str] | None = None, metadata: dict | None = None,
) -> KnowledgeDocument:
    if len(payload) > _MAX_UPLOAD_BYTES:
        raise ValueError(
            f"File is too large ({len(payload):,} bytes) -- the per-upload limit is "
            f"{_MAX_UPLOAD_BYTES:,} bytes ({_MAX_UPLOAD_BYTES // (1024 * 1024)}MB)."
        )
    existing = len(list_documents(owner, limit=_MAX_DOCS_PER_OWNER + 1))
    if existing >= _MAX_DOCS_PER_OWNER:
        raise ValueError(
            f"Your personal knowledge base already has {existing} documents, at the limit of "
            f"{_MAX_DOCS_PER_OWNER} -- delete an existing document before uploading a new one."
        )
    text, source_type = knowledge_store.extract_text(filename, payload)
    if not text:
        raise ValueError("No text could be extracted from the document")
    checksum = hashlib.sha256(payload).hexdigest()
    doc_id = "pkdoc_" + uuid.uuid4().hex[:16]
    now = _now()
    chunks_text = knowledge_store._chunk_text(text)

    # Best-effort: embed every chunk in one batch call. None (unconfigured, or the
    # call failed) means every chunk's `embedding` stays None -- search() below
    # falls back to TF-IDF for this owner's whole set until a re-ingest succeeds.
    embeddings = embeddings_client.embed_texts(chunks_text)

    doc = KnowledgeDocument(
        document_id=doc_id, owner=owner, title=(title or filename or "Document").strip(),
        filename=filename or "document.txt", source_type=source_type,
        classification=sorted(set(classification or [INTERNAL])), created_at=now, updated_at=now,
        chunk_count=len(chunks_text), checksum=checksum, metadata=dict(metadata or {}),
    )
    chunks = [
        KnowledgeChunk(
            chunk_id=f"{doc_id}_c{idx:04d}", document_id=doc_id, owner=owner, ordinal=idx, text=chunk_text,
            terms=knowledge_store._terms(chunk_text), embedding=(embeddings[idx] if embeddings else None),
        )
        for idx, chunk_text in enumerate(chunks_text)
    ]

    if _cosmos_configured():
        docs_c, chunks_c = _cosmos_containers()
        docs_c.upsert_item(_doc_to_item(doc))
        for chunk in chunks:
            chunks_c.upsert_item(_chunk_to_item(chunk))
    else:
        data = _local_read()
        data.setdefault("documents", {})[doc_id] = _doc_to_item(doc)
        for chunk in chunks:
            data.setdefault("chunks", {})[chunk.chunk_id] = _chunk_to_item(chunk)
        _local_write(data)
    return doc


# ── reads ───────────────────────────────────────────────────────────────────

def list_documents(owner: str, limit: int = 100) -> list[KnowledgeDocument]:
    if _cosmos_configured():
        docs_c, _ = _cosmos_containers()
        items = docs_c.query_items(
            query="SELECT * FROM c WHERE c.owner = @owner", parameters=[{"name": "@owner", "value": owner}],
            partition_key=owner,
        )
        docs = [_doc_from_item(d) for d in items]
    else:
        data = _local_read()
        docs = [_doc_from_item(d) for d in data.get("documents", {}).values() if d.get("owner") == owner]
    docs.sort(key=lambda d: d.created_at, reverse=True)
    return docs[: max(1, int(limit or 100))]


def get_document(document_id: str, owner: str) -> KnowledgeDocument | None:
    """Owner-scoped by construction -- there is no admin/all-owners variant of
    this function anywhere in this module."""
    if _cosmos_configured():
        docs_c, _ = _cosmos_containers()
        from azure.cosmos import exceptions as cosmos_exceptions

        try:
            return _doc_from_item(docs_c.read_item(item=document_id, partition_key=owner))
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return None
    data = _local_read()
    raw = data.get("documents", {}).get(document_id)
    if not raw or raw.get("owner") != owner:
        return None
    return _doc_from_item(raw)


def _chunks_for(owner: str, document_id: str | None = None) -> list[KnowledgeChunk]:
    if _cosmos_configured():
        _, chunks_c = _cosmos_containers()
        items = chunks_c.query_items(
            query="SELECT * FROM c WHERE c.owner = @owner", parameters=[{"name": "@owner", "value": owner}],
            partition_key=owner,
        )
        chunks = [_chunk_from_item(c) for c in items]
    else:
        data = _local_read()
        chunks = [_chunk_from_item(c) for c in data.get("chunks", {}).values() if c.get("owner") == owner]
    if document_id:
        chunks = [c for c in chunks if c.document_id == document_id]
    return chunks


# ── delete ──────────────────────────────────────────────────────────────────

def delete_document(document_id: str, owner: str) -> bool:
    if _cosmos_configured():
        docs_c, chunks_c = _cosmos_containers()
        from azure.cosmos import exceptions as cosmos_exceptions

        try:
            docs_c.delete_item(document_id, partition_key=owner)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return False
        for chunk in _chunks_for(owner, document_id):
            try:
                chunks_c.delete_item(chunk.chunk_id, partition_key=owner)
            except cosmos_exceptions.CosmosResourceNotFoundError:
                pass
        return True

    data = _local_read()
    raw = data.get("documents", {}).get(document_id)
    if not raw or raw.get("owner") != owner:
        return False
    del data["documents"][document_id]
    for cid in [k for k, v in data.get("chunks", {}).items() if v.get("document_id") == document_id]:
        del data["chunks"][cid]
    _local_write(data)
    return True


def delete_all_for_owner(owner: str) -> int:
    """Cascade-delete hook for consumer offboarding (see gateway/backend/
    admin_policy.py's delete_consumer handler) -- not an admin-read/browse path.
    Returns the number of documents removed."""
    docs = list_documents(owner, limit=10_000)
    for doc in docs:
        delete_document(doc.document_id, owner)
    return len(docs)


# ── search / answer ─────────────────────────────────────────────────────────

def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _tfidf_scored(chunks: list[KnowledgeChunk], query: str) -> list[tuple[float, KnowledgeChunk]]:
    """Same algorithm as knowledge_store.search()'s TF-IDF scoring, over this
    module's chunk objects -- the degrade-gracefully path when embeddings are
    unconfigured, a call fails, or a chunk predates embeddings being turned on."""
    q_terms = knowledge_store._terms(query or "")
    if not q_terms:
        return []
    doc_count = max(1, len({c.document_id for c in chunks}))
    doc_freq: dict[str, int] = {}
    for c in chunks:
        for term in set(c.terms):
            doc_freq[term] = doc_freq.get(term, 0) + 1
    scored = []
    for c in chunks:
        score = 0.0
        length_norm = 1.0 + math.log(1 + max(1, sum(c.terms.values())))
        for term, qf in q_terms.items():
            tf = c.terms.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + doc_count / (1 + doc_freq.get(term, 0)))
            score += (1 + math.log(tf)) * idf * qf / length_norm
        if score > 0:
            scored.append((score, c))
    return scored


def _first_stage_order(chunks: list[KnowledgeChunk], query: str, use_embeddings: bool = True) -> list[tuple[float, KnowledgeChunk]]:
    """Cosine-over-embeddings order (falling back to TF-IDF if embeddings are
    unconfigured or every chunk predates embeddings), best first. Used both to
    narrow a large corpus to a candidate pool before reranking, and as the
    fallback order if the rerank call itself fails.

    `use_embeddings=False` skips the embed_query() call entirely and goes
    straight to TF-IDF -- see search()'s small-corpus path, where this order
    is only ever needed as a rerank-failure fallback, not for narrowing, so
    paying for a live embedding call on every such query buys nothing most of
    the time (rerank succeeds) for a corpus that's going to the reranker in
    full regardless."""
    if use_embeddings:
        query_embedding = embeddings_client.embed_query(query)
        embedded_chunks = [c for c in chunks if c.embedding]
    else:
        query_embedding, embedded_chunks = None, []
    if query_embedding is not None and embedded_chunks:
        scored = [(s, c) for s, c in ((_cosine(query_embedding, c.embedding), c) for c in embedded_chunks) if s > 0]
    else:
        scored = _tfidf_scored(chunks, query)
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def search(owner: str, query: str, limit: int = 5, document_id: str | None = None) -> list[dict]:
    chunks = _chunks_for(owner, document_id)
    if not chunks:
        return []
    limit = max(1, int(limit or 5))

    # Below the vectorize threshold, the whole corpus goes straight to the
    # reranker (see below) and this order is only ever used as the
    # rerank-failure fallback -- skip the per-query embed_query() call in
    # that case (free TF-IDF fallback instead) and only pay for a live
    # embedding call when it's actually doing narrowing work on a larger
    # corpus. Configurable via GOVERNANCE_PERSONAL_KB_VECTORIZE_THRESHOLD.
    small_corpus = len(chunks) <= _VECTORIZE_THRESHOLD_CHUNKS
    fallback = _first_stage_order(chunks, query, use_embeddings=not small_corpus)
    if small_corpus:
        candidates = chunks  # send the whole corpus to the reranker, unfiltered
    else:
        candidates = [c for _, c in fallback[:_RERANK_CANDIDATE_POOL]]

    reranked = (
        rerank_client.rerank(query, [c.text for c in candidates], top_n=limit)
        if len(candidates) - limit > _RERANK_SKIP_MARGIN
        else None
    )
    if reranked is not None:
        n = max(1, len(reranked))
        scored = [(1.0 - pos / n, candidates[i]) for pos, i in enumerate(reranked) if i < len(candidates)]
    else:
        scored = fallback[:limit]

    if _cosmos_configured():
        docs_c, _ = _cosmos_containers()

        def _doc_lookup(did: str) -> dict:
            from azure.cosmos import exceptions as cosmos_exceptions

            try:
                return docs_c.read_item(item=did, partition_key=owner)
            except cosmos_exceptions.CosmosResourceNotFoundError:
                return {}
    else:
        _data = _local_read().get("documents", {})

        def _doc_lookup(did: str) -> dict:
            return _data.get(did, {})

    results = []
    for score, chunk in scored[:limit]:
        doc = _doc_lookup(chunk.document_id)
        item = chunk.public_dict(score=score)
        item.update({
            "documentTitle": doc.get("title", chunk.document_id),
            "filename": doc.get("filename", ""),
            "classification": list(doc.get("classification") or [INTERNAL]),
        })
        results.append(item)
    return results


def answer(owner: str, query: str, limit: int = 5, document_id: str | None = None) -> dict:
    matches = search(owner, query, limit, document_id)
    if not matches:
        return {"answer": "No matching personal document content was found.", "citations": []}
    lines = [f"{i}. {m['documentTitle']} chunk {m['ordinal'] + 1}: {m['text']}" for i, m in enumerate(matches, 1)]
    return {
        "answer": "Relevant personal-document context:\n" + "\n".join(lines),
        "citations": [
            {"documentId": m["documentId"], "chunkId": m["chunkId"], "documentTitle": m["documentTitle"], "score": m["score"]}
            for m in matches
        ],
    }
