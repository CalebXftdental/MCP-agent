from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class KnowledgeDocument:
    document_id: str
    owner: str
    title: str
    filename: str
    source_type: str
    classification: list[str]
    created_at: float
    updated_at: float
    chunk_count: int = 0
    checksum: str = ""
    metadata: dict = field(default_factory=dict)

    def public_dict(self) -> dict:
        return {
            "documentId": self.document_id,
            "owner": self.owner,
            "title": self.title,
            "filename": self.filename,
            "sourceType": self.source_type,
            "classification": list(self.classification),
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "chunkCount": self.chunk_count,
            "checksum": self.checksum,
            "metadata": dict(self.metadata or {}),
        }


@dataclass
class KnowledgeChunk:
    chunk_id: str
    document_id: str
    owner: str
    ordinal: int
    text: str
    terms: dict[str, int]
    embedding: list[float] | None = None  # personal-KB only; None for company-tier
    # local-fallback chunks, which stay TF-IDF-only (see personal_knowledge_store.py)

    def public_dict(self, score: float | None = None, max_chars: int = 700) -> dict:
        snippet = self.text.strip()
        if len(snippet) > max_chars:
            snippet = snippet[: max_chars - 1].rstrip() + "..."
        data = {
            "chunkId": self.chunk_id,
            "documentId": self.document_id,
            "ordinal": self.ordinal,
            "text": snippet,
        }
        if score is not None:
            data["score"] = round(score, 4)
        return data
