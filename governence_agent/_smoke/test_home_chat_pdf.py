"""End-to-end proof that HOME chat can produce a content-bearing PDF.

Simulates the self-hosted Qwen/sglang backend (text-format tool calls, the
default local LLM path -- see orchestrator.py's module docstring) asking for a
customer PDF, run through the REAL orchestrator.run_chat loop against a REAL
mcp-office backend. No mocking of _govern/mcp/tool execution -- only the LLM
call itself is faked, since we don't have a live Qwen server in this env.

This is the regression check for the `_coerce` fix in orchestrator.py: before
that fix, the JSON array the "model" gives for `sections`/`tables` arrived at
the tool as a plain string and the call failed/produced an empty artifact.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "home-chat-pdf"
OUT = ROOT / "_smoke" / ".tmp" / "home-chat-pdf-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "chat_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "chat_password",
    "GOVERNANCE_SESSION_SECRET": "home-chat-pdf-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18291/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "10",
})

PASS, FAIL = 0, 0


def check(name, cond, detail=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}", detail if detail is not None else "")


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


def pdf_text(payload: bytes) -> str:
    raw = payload.decode("latin-1", errors="replace")
    return "\n".join(m.group(1).replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
                      for m in re.finditer(r"\(((?:[^()\\]|\\.)*)\)\s*Tj", raw))


print("start office backend")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18291", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18291/health")
    check("office backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    import orchestrator  # noqa: E402
    import request_context as ctx  # noqa: E402
    from mcp_server import mcp  # noqa: E402
    import app as gateway_app  # noqa: E402,F401 -- registers the @mcp.tool defs (incl. office_create_pdf_packet) on mcp_server.mcp
    from store import get_store  # noqa: E402
    from store.models import ConsumerRecord  # noqa: E402
    from auth.passwords import hash_password  # noqa: E402

    store = get_store()
    record = ConsumerRecord(
        consumer_id="user:chat_customer_pdf", name="chat_customer_pdf", key_hash="", status="active",
        role="user", type="user", categories=["office"],
        login_password_hash=hash_password("chat_password"),
    )
    store.upsert_consumer(record)
    # Same principal-binding /chat itself does before calling orchestrator.run_chat
    # (backend/chat.py) -- the gateway's office_create_pdf_packet tool takes no
    # `owner` parameter at all; it always reads ctx.consumer_ctx.get() server-side,
    # so a real chat turn (or this simulation) must set it first.
    ctx.consumer_ctx.set(record.name)
    ctx.consumer_record_ctx.set(record)
    ctx.ip_ctx.set("127.0.0.1")

    # ── Simulate the self-hosted Qwen/sglang backend ────────────────────────
    # Turn 1: the model asks to build the PDF, with real structured content it
    # authored itself (a real model would draft this from data it already has
    # in context, or from a prior tool result -- content is not the point of
    # this test, correct plumbing of array/object args is).
    sections = [
        {"heading": "Executive Summary", "bullets": [
            "Customer: Sample Dental Group",
            "Orders reviewed: 4",
            "Total spend in scope: 12345.67",
        ]},
        {"heading": "Recommended Next Steps", "bullets": [
            "Review open orders and shipment exceptions.",
        ]},
    ]
    tables = [{"name": "Orders", "rows": [
        {"orderNumber": "SO-1001", "status": "Completed", "total": 4200.00},
        {"orderNumber": "SO-1003", "status": "Open", "total": 1995.17},
    ]}]
    turn1 = (
        "<tool_call><function=office_create_pdf_packet>"
        "<parameter=title>Customer 360 - Sample Dental Group</parameter>"
        f"<parameter=sections>{json.dumps(sections)}</parameter>"
        f"<parameter=tables>{json.dumps(tables)}</parameter>"
        "<parameter=classification>[\"INTERNAL\"]</parameter>"
        "</function></tool_call>"
    )
    turn2 = "Here's the customer 360 PDF packet for Sample Dental Group, built from the account data on file."

    calls = {"n": 0}

    def fake_llm_complete(messages, tools):
        calls["n"] += 1
        content = turn1 if calls["n"] == 1 else turn2

        class Msg:
            tool_calls = None

        m = Msg()
        m.content = content
        return m

    result = asyncio.run(orchestrator.run_chat(
        mcp, "Generate a PDF report on Sample Dental Group's account.", "chat:test",
        record, llm_complete=fake_llm_complete,
    ))
    check("chat loop completed without error", bool(result.get("reply")), result)
    check("exactly one tool call was made", len(result.get("tool_calls", [])) == 1, result.get("tool_calls"))
    tool_call = (result.get("tool_calls") or [{}])[0]
    check("the tool call was office_create_pdf_packet", tool_call.get("tool") == "office_create_pdf_packet", tool_call)
    check("sections arrived as a real list, not a string", isinstance(tool_call.get("args", {}).get("sections"), list), tool_call)
    check("tables arrived as a real list, not a string", isinstance(tool_call.get("args", {}).get("tables"), list), tool_call)

    # Pull the artifact the tool call actually produced, straight from the store
    # (run_chat's own return value only carries {tool, args}, not the tool's result).
    import artifact_store
    records = list(artifact_store.list_artifacts(owner=record.name))
    check("an artifact was actually created", bool(records), records)
    if records:
        art = sorted(records, key=lambda r: r.created_at)[-1]
        payload = Path(art.storage_path).read_bytes()
        (OUT / "home-chat-customer-360.pdf").write_bytes(payload)
        text = pdf_text(payload)
        for needle in ("Customer 360 - Sample Dental Group", "Executive Summary", "Orders reviewed: 4", "SO-1001", "4200"):
            check(f"PDF contains {needle!r}", needle in text, text[:400])
        check("PDF has real content, not just a title (regression check)", len(text.splitlines()) > 5, text)
finally:
    office.terminate()
    try:
        office.wait(timeout=5)
    except subprocess.TimeoutExpired:
        office.kill()

print(f"\n{PASS} passed, {FAIL} failed")
print(f"artifacts written to: {OUT}")
sys.exit(1 if FAIL else 0)
