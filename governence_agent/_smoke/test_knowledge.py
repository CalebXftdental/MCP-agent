"""Local HTTP smoke for the read-only governed knowledge search/Q&A backend.

Starts mcp-knowledge (plus a fake AraTestEnvBE-shaped RAG service standing in
for the real one), imports the gateway app, logs in as a seeded admin, and
checks: search/answer proxy to the shared remote knowledge base, the ingest
tools/routes no longer exist at all, and existing local documents (seeded
directly, not through any exposed write path) can still be listed/deleted.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "knowledge"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "knowledge_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "knowledge_password",
    "GOVERNANCE_SESSION_SECRET": "knowledge-smoke-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_KNOWLEDGE_STORE_FILE": str(TMP / "state" / "knowledge.json"),
    "KNOWLEDGE_MCP_URL": "http://127.0.0.1:18050/mcp",
    "KNOWLEDGE_RETRIEVAL_BASE_URL": "http://127.0.0.1:18051",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "10",
})

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def wait_health(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"service did not become healthy: {last}")


# Stands in for AraTestEnvBE's src/ragAgent/api/app.py -- same route shapes
# (/api/ai-search/test-retriever, /api/ai-search/chat), fake data only. No
# /api/ai-search/upload here at all -- this backend is read-only, so there's
# nothing that would ever call it.
FAKE_RAG_APP = TMP / "fake_rag_app.py"
FAKE_RAG_APP.write_text("""
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

async def health(request):
    return JSONResponse({'ok': True})

async def retrieve(request):
    payload = await request.json()
    q = payload.get('query', '')
    return JSONResponse({'query': q, 'results': [{'id': 'remote-1', 'chunk_id': 'remote-c1', 'chunk_index': 0, 'filename': 'remote-policy.pdf', 'page': 4, 'type': 'text', 'content': 'Remote RAG says executive briefing is required for high-risk renewals.', 'score': 0.91}], 'num_results': 1, 'retrieval_time_ms': 3.2})

async def chat(request):
    return JSONResponse({'response': 'Remote answer: executive briefing is required for high-risk renewals.', 'context_chunks': [{'chunk_id': 'remote-c1', 'chunk_index': 0, 'filename': 'remote-policy.pdf', 'page': 4, 'type': 'text', 'content': 'Remote RAG says executive briefing is required for high-risk renewals.', 'score': 0.91}]})

app = Starlette(routes=[
    Route('/health', health),
    Route('/api/ai-search/test-retriever', retrieve, methods=['POST']),
    Route('/api/ai-search/chat', chat, methods=['POST']),
])
""", encoding="utf-8")

print("start fake AraTestEnvBE-shaped RAG service")
print("start knowledge backend")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
if not python_exe.exists():
    python_exe = Path(sys.executable)
fake_rag = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "fake_rag_app:app", "--host", "127.0.0.1", "--port", "18051", "--no-access-log"],
    cwd=str(TMP),
    env=os.environ.copy(),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
knowledge = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18050", "--no-access-log"],
    cwd=str(ROOT / "mcp-knowledge"),
    env={**os.environ, "KNOWLEDGE_ENV_FILE": os.devnull},  # ignore any real mcp-knowledge/.env.local for this test
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18051/health")
    check("fake RAG service healthy", True)
    wait_health("http://127.0.0.1:18050/health")
    check("knowledge backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")
    from policy import manifest
    from policy.categories import get_category
    import knowledge_store

    check("knowledge backend registered", "knowledge" in manifest.backends())
    check("knowledge category exists", get_category("knowledge") is not None)
    check("knowledge tool is namespaced", manifest.namespaced("search_knowledge") == "knowledge_search_knowledge")
    check("no ingest_knowledge_text tool in the manifest (read-only)", manifest.get("ingest_knowledge_text") is None)
    check("no ingest_knowledge_file tool in the manifest (read-only)", manifest.get("ingest_knowledge_file") is None)
    check("knowledge category no longer grants any ingest tool", not ({"ingest_knowledge_text", "ingest_knowledge_file"} & manifest.tools_for_backend("knowledge")))

    # Seed a document directly via the local store's Python API -- simulating
    # "already existed locally before/independent of this gateway" rather than
    # anything added through an exposed route or tool.
    knowledge_store.reload_for_tests()
    seeded = knowledge_store.ingest_text(
        "knowledge_admin", "Renewal Escalation Policy",
        "Renewal escalation policy: customers with delayed shipments over 14 days require manager review.",
        ["INTERNAL"],
    )
    check("seed document created directly via the store (not through any route/tool)", bool(seeded.document_id))

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        login = client.post("/dashboard/login", json={"username": "knowledge_admin", "password": "knowledge_password"})
        check("login succeeds", login.status_code == 200)

        posted = client.post("/knowledge/documents", json={"title": "Should not work", "text": "anything"})
        check("POST /knowledge/documents no longer exists (405)", posted.status_code == 405, posted.text)

        listed = client.get("/knowledge/documents")
        check("GET /knowledge/documents still lists pre-existing local documents", listed.status_code == 200 and any(d.get("documentId") == seeded.document_id for d in listed.json().get("documents", [])), listed.text)

        search = client.post("/knowledge/search", json={"query": "delayed shipments manager review", "limit": 3})
        results = search.json().get("results") or []
        check("dashboard search returns remote RAG result", search.status_code == 200 and results and search.json().get("mode") == "remote", search.text)
        check("search snippet includes remote briefing", "executive briefing" in results[0].get("text", "").lower(), results[:1])

        answer = client.post("/knowledge/answer", json={"query": "What requires manager review?", "limit": 3})
        body = answer.json()
        check("dashboard answer has citation", answer.status_code == 200 and body.get("citations"), answer.text)
        check("dashboard answer uses remote RAG context", "executive briefing" in body.get("answer", "").lower(), body.get("answer"))

        tool_ingest = client.post("/dashboard/try-tool", json={
            "tool": "ingest_knowledge_file",
            "args": {"title": "Should not work", "filename": "x.txt", "content_base64": "aGk=", "classification": ["INTERNAL"]},
        })
        check("playground rejects the removed ingest_knowledge_file tool", tool_ingest.status_code == 400 and "unknown tool" in tool_ingest.json().get("error", ""), tool_ingest.text)

        tool_search = client.post("/dashboard/try-tool", json={"tool": "search_knowledge", "args": {"query": "executive briefing", "limit": 2}})
        tool_search_result = tool_search.json().get("result") or {}
        check("playground search uses remote adapter", tool_search.status_code == 200 and tool_search_result.get("mode") == "remote" and tool_search_result.get("results"), tool_search.text)

        deleted = client.delete(f"/knowledge/documents/{seeded.document_id}")
        check("document delete still works (local cleanup, not a write to the shared KB)", deleted.status_code == 200 and deleted.json().get("ok"))
finally:
    knowledge.terminate()
    fake_rag.terminate()
    try:
        knowledge.wait(timeout=5)
    except subprocess.TimeoutExpired:
        knowledge.kill()
    try:
        fake_rag.wait(timeout=5)
    except subprocess.TimeoutExpired:
        fake_rag.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
