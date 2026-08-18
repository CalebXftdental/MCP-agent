"""Azure AI Document Intelligence client -- PDF text extraction for ALL PDFs, not
just scanned ones (replaces a local PDF-parsing library entirely).

Revised into the design after the original plan (a local library -- pypdf/
pdfplumber/PyMuPDF -- with Document Intelligence only as an OCR fallback for
scanned pages) ran into PyMuPDF's AGPL license. Document Intelligence's
prebuilt-read model extracts text from any PDF, text-layer or scanned, and is
generally more reliable than the local candidates on multi-column layout, tables,
and broken font/encoding maps (it's a trained layout model, not a positional-glyph
heuristic) -- see digest_persoanl_kb.md §0.1 for the full tradeoff (every PDF's
content now leaves the app to Azure, not just scanned ones; ingestion gains a hard
dependency on Document Intelligence's availability).

Lazy-imports azure.ai.documentintelligence so a process with no Document
Intelligence config configured pays no import cost. Returns "" on any failure
(missing config, network/API error, timeout) -- callers treat empty text as
"nothing extracted", the same outcome as today's placeholder decode, rather than
raising and crashing ingestion.
"""
from __future__ import annotations

import io
import os


def configured() -> bool:
    return bool(os.getenv("GOVERNANCE_DOCINTEL_ENDPOINT") and os.getenv("GOVERNANCE_DOCINTEL_API_KEY"))


def _timeout() -> float:
    return float(os.getenv("GOVERNANCE_DOCINTEL_TIMEOUT_SEC") or "60")


def extract_pdf_text(payload: bytes) -> str:
    """Full document text, reading-order reconstructed by the prebuilt-read model.
    "" if unconfigured, or on any error -- never raises."""
    if not payload or not configured():
        return ""
    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.core.credentials import AzureKeyCredential

        client = DocumentIntelligenceClient(
            endpoint=(os.getenv("GOVERNANCE_DOCINTEL_ENDPOINT") or "").rstrip("/"),
            credential=AzureKeyCredential(os.getenv("GOVERNANCE_DOCINTEL_API_KEY")),
        )
        poller = client.begin_analyze_document("prebuilt-read", body=io.BytesIO(payload))
        result = poller.result(timeout=_timeout())
        return (result.content or "").strip()
    except Exception:
        return ""
