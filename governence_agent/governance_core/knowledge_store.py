from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import docintel_client
from knowledge_models import KnowledgeChunk, KnowledgeDocument
from policy.manifest import INTERNAL

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.'-]*")
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def _store_file() -> Path:
    explicit = os.getenv("GOVERNANCE_KNOWLEDGE_STORE_FILE")
    if explicit:
        return Path(explicit)
    state_dir = Path(os.getenv("GOVERNANCE_STATE_DIR") or ".governance-state")
    return state_dir / "knowledge.json"


def _now() -> float:
    return time.time()


def _terms(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in _WORD_RE.finditer(text.lower()):
        token = m.group(0).strip(".'-")
        if len(token) < 2:
            continue
        out[token] = out.get(token, 0) + 1
    return out


def _normalize_text(text: str) -> str:
    return _SPACE_RE.sub(" ", text.replace("\x00", " ")).strip()


def _xml_text(raw: bytes) -> str:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return ""
    parts = []
    for node in root.iter():
        if node.text and node.text.strip():
            parts.append(node.text.strip())
    return _normalize_text(" ".join(parts))


def extract_text(filename: str, payload: bytes) -> tuple[str, str]:
    suffix = Path(filename or "document.txt").suffix.lower().lstrip(".") or "txt"
    if suffix in {"txt", "md", "csv", "tsv", "json", "log"}:
        return _normalize_text(payload.decode("utf-8", "ignore")), suffix
    if suffix == "docx":
        with zipfile.ZipFile(__import__("io").BytesIO(payload)) as z:
            return _xml_text(z.read("word/document.xml")) if "word/document.xml" in z.namelist() else "", suffix
    if suffix == "pptx":
        with zipfile.ZipFile(__import__("io").BytesIO(payload)) as z:
            names = sorted(n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
            return _normalize_text(" ".join(_xml_text(z.read(n)) for n in names)), suffix
    if suffix == "xlsx":
        with zipfile.ZipFile(__import__("io").BytesIO(payload)) as z:
            names = z.namelist()
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                try:
                    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
                    shared = [" ".join(t.text or "" for t in si.iter() if t.tag.endswith('}t') or t.tag == 't') for si in root]
                except ET.ParseError:
                    shared = []
            parts = []
            for n in sorted(x for x in names if x.startswith("xl/worksheets/sheet") and x.endswith(".xml")):
                xml = z.read(n).decode("utf-8", "ignore")
                parts.append(_TAG_RE.sub(" ", xml))
            return _normalize_text(" ".join(shared + parts)), suffix
    if suffix == "pdf":
        # Azure AI Document Intelligence (prebuilt-read), not a local PDF-parsing
        # library -- see docintel_client.py's docstring and digest_persoanl_kb.md
        # §0.1 for why. "" (not an exception) when unconfigured/unreachable, same
        # as any other extraction miss -- ingest_document() below raises on empty
        # text either way.
        return _normalize_text(docintel_client.extract_pdf_text(payload)), suffix
    return _normalize_text(payload.decode("utf-8", "ignore")), suffix


def _chunk_text(text: str, chunk_chars: int = 1200, overlap: int = 180) -> list[str]:
    text = _normalize_text(text)
    if not text:
        return []
    chunk_chars = max(300, int(chunk_chars or 1200))
    overlap = max(0, min(int(overlap or 0), chunk_chars // 2))
    chunks = []
    pos = 0
    while pos < len(text):
        end = min(len(text), pos + chunk_chars)
        if end < len(text):
            split = max(text.rfind(". ", pos, end), text.rfind("\n", pos, end), text.rfind(" ", pos, end))
            if split > pos + chunk_chars // 2:
                end = split + 1
        chunk = text[pos:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        pos = max(end - overlap, pos + 1)
    return chunks


def _read() -> dict:
    path = _store_file()
    if not path.exists():
        return {"documents": {}, "chunks": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"documents": {}, "chunks": {}}


def _write(data: dict) -> None:
    path = _store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8", newline="")
    tmp.replace(path)


def _doc_from_dict(d: dict) -> KnowledgeDocument:
    return KnowledgeDocument(
        document_id=d["document_id"], owner=d["owner"], title=d.get("title", d.get("filename", "Document")),
        filename=d.get("filename", "document.txt"), source_type=d.get("source_type", "txt"),
        classification=list(d.get("classification") or [INTERNAL]), created_at=float(d.get("created_at") or 0),
        updated_at=float(d.get("updated_at") or d.get("created_at") or 0), chunk_count=int(d.get("chunk_count") or 0),
        checksum=d.get("checksum", ""), metadata=dict(d.get("metadata") or {}),
    )


def _chunk_from_dict(d: dict) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=d["chunk_id"], document_id=d["document_id"], owner=d["owner"],
        ordinal=int(d.get("ordinal") or 0), text=d.get("text", ""), terms=dict(d.get("terms") or {}),
    )


def ingest_document(owner: str, title: str, filename: str, payload: bytes, classification: list[str] | None = None, metadata: dict | None = None) -> KnowledgeDocument:
    text, source_type = extract_text(filename, payload)
    if not text:
        raise ValueError("No text could be extracted from the document")
    checksum = hashlib.sha256(payload).hexdigest()
    data = _read()
    doc_id = "kdoc_" + uuid.uuid4().hex[:16]
    chunks_text = _chunk_text(text)
    now = _now()
    doc = KnowledgeDocument(
        document_id=doc_id, owner=owner, title=(title or filename or "Document").strip(), filename=filename or "document.txt",
        source_type=source_type, classification=sorted(set(classification or [INTERNAL])), created_at=now, updated_at=now,
        chunk_count=len(chunks_text), checksum=checksum, metadata=dict(metadata or {}),
    )
    data.setdefault("documents", {})[doc_id] = doc.__dict__
    for idx, chunk_text in enumerate(chunks_text):
        chunk_id = f"{doc_id}_c{idx:04d}"
        data.setdefault("chunks", {})[chunk_id] = KnowledgeChunk(chunk_id, doc_id, owner, idx, chunk_text, _terms(chunk_text)).__dict__
    _write(data)
    return doc


def record_external_document(owner: str, title: str, filename: str, source_type: str, classification: list[str] | None = None, metadata: dict | None = None, chunk_count: int = 0, checksum_payload: bytes | None = None) -> KnowledgeDocument:
    data = _read()
    doc_id = "kdoc_" + uuid.uuid4().hex[:16]
    now = _now()
    checksum = hashlib.sha256(checksum_payload or (filename or title).encode("utf-8")).hexdigest()
    doc = KnowledgeDocument(
        document_id=doc_id, owner=owner, title=(title or filename or "Document").strip(), filename=filename or "document",
        source_type=source_type or Path(filename or "document").suffix.lower().lstrip(".") or "external",
        classification=sorted(set(classification or [INTERNAL])), created_at=now, updated_at=now,
        chunk_count=int(chunk_count or 0), checksum=checksum, metadata=dict(metadata or {}),
    )
    data.setdefault("documents", {})[doc_id] = doc.__dict__
    _write(data)
    return doc


def ingest_text(owner: str, title: str, text: str, classification: list[str] | None = None, filename: str = "") -> KnowledgeDocument:
    return ingest_document(owner, title, filename or f"{title or 'document'}.txt", (text or "").encode("utf-8"), classification, {"ingest": "text"})


def list_documents(owner: str | None = None, limit: int = 100) -> list[KnowledgeDocument]:
    docs = [_doc_from_dict(d) for d in _read().get("documents", {}).values()]
    if owner:
        docs = [d for d in docs if d.owner == owner]
    docs.sort(key=lambda d: d.created_at, reverse=True)
    return docs[: max(1, int(limit or 100))]


def get_document(document_id: str) -> KnowledgeDocument | None:
    raw = _read().get("documents", {}).get(document_id)
    return _doc_from_dict(raw) if raw else None


def delete_document(document_id: str, owner: str | None = None) -> bool:
    data = _read()
    raw = data.get("documents", {}).get(document_id)
    if not raw or (owner and raw.get("owner") != owner):
        return False
    del data["documents"][document_id]
    for cid in [k for k, v in data.get("chunks", {}).items() if v.get("document_id") == document_id]:
        del data["chunks"][cid]
    _write(data)
    return True


def search(owner: str, query: str, limit: int = 5, document_id: str | None = None) -> list[dict]:
    q_terms = _terms(query or "")
    if not q_terms:
        return []
    data = _read()
    docs = data.get("documents", {})
    chunks = [_chunk_from_dict(c) for c in data.get("chunks", {}).values()]
    chunks = [c for c in chunks if c.owner == owner and (not document_id or c.document_id == document_id)]
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
            doc = docs.get(c.document_id, {})
            item = c.public_dict(score=score)
            item.update({
                "documentTitle": doc.get("title", c.document_id),
                "filename": doc.get("filename", ""),
                "classification": list(doc.get("classification") or [INTERNAL]),
            })
            scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[: max(1, int(limit or 5))]]


def answer(owner: str, query: str, limit: int = 5, document_id: str | None = None) -> dict:
    matches = search(owner, query, limit, document_id)
    if not matches:
        return {"answer": "No matching indexed document content was found.", "citations": []}
    lines = []
    for i, m in enumerate(matches, 1):
        lines.append(f"{i}. {m['documentTitle']} chunk {m['ordinal'] + 1}: {m['text']}")
    return {
        "answer": "Relevant indexed context:\n" + "\n".join(lines),
        "citations": [{"documentId": m["documentId"], "chunkId": m["chunkId"], "documentTitle": m["documentTitle"], "score": m["score"]} for m in matches],
    }


def reload_for_tests() -> None:
    return None
