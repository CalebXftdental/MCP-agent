"""Proves the propose_graph wire contract: a /workflow-chat turn whose model
calls propose_graph comes back with tool_calls[].result carrying the REAL
tool JSON (status/graphId), not just args -- and that graphId is for a real,
retrievable draft graph. (The frontend used to render an animated preview of
this off the same contract -- WorkflowProposalReveal, removed as redundant
once the copilot's own reply plus the workflow dropdown already covered
"go open your draft" -- but the contract itself is still worth protecting for
whatever reads tool_calls[].result next.) Drives the real HTTP route with a
fake LLM (no live model needed) that emits exactly one propose_graph tool
call, Qwen-text format, same shape a real sglang turn uses.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "workflow-proposal-reveal"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "reveal_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "reveal_password",
    "GOVERNANCE_SESSION_SECRET": "workflow-proposal-reveal-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
})

PASS, FAIL = 0, 0


def check(name, cond, detail=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}", str(detail)[:600] if detail is not None else "")


sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))
gateway_app = importlib.import_module("app")
import orchestrator  # noqa: E402
import workflow_graph_store  # noqa: E402
from store import get_store  # noqa: E402
from store.models import ConsumerRecord  # noqa: E402
from auth.passwords import hash_password  # noqa: E402

store = get_store()
store.upsert_consumer(ConsumerRecord(
    consumer_id="user:reveal_tester", name="reveal_tester", key_hash="", status="active",
    role="user", type="user", categories=["office"],
    login_password_hash=hash_password("tester_password"),
))

PROPOSED_NODES = [
    {"nodeId": "t1", "kind": "trigger"},
    {"nodeId": "n1", "kind": "tool_call", "tool": "create_pdf_packet", "title": "Build the report",
     "config": {"title": "Test report", "sections": [{"heading": "H", "bullets": ["a"]}], "classification": ["INTERNAL"]}},
]
PROPOSED_EDGES = [{"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}]

TURN1 = (
    "<tool_call><function=propose_graph>"
    "<parameter=display_name>Copilot demo workflow</parameter>"
    f"<parameter=nodes>{json.dumps(PROPOSED_NODES)}</parameter>"
    f"<parameter=edges>{json.dumps(PROPOSED_EDGES)}</parameter>"
    "</function></tool_call>"
)
TURN2 = "I've put together a draft for you to review in the workflow dropdown."

class _FakeMsg:
    tool_calls = None

    def __init__(self, content):
        self.content = content


def fake_complete(messages, tools):
    # Driven by conversation STATE (not a global call counter, which would
    # break across the two separate HTTP requests this test makes): once the
    # tool has actually run, the text-tool-call loop's last message is the
    # wrapped <tool_response>; before that, it's the user's fresh ask.
    last = messages[-1] if messages else {}
    if last.get("role") == "user" and "<tool_response>" in str(last.get("content") or ""):
        return _FakeMsg(TURN2)
    return _FakeMsg(TURN1)


orchestrator.default_llm_complete = lambda: fake_complete

with TestClient(gateway_app.app, base_url="http://testserver") as client:
    client.post("/dashboard/login", json={"username": "reveal_tester", "password": "tester_password"})

    resp = client.post("/workflow-chat", json={"message": "build me a report and propose a workflow for it"})
    check("/workflow-chat responds", resp.status_code == 200, resp.text)
    body = resp.json()
    check("assistant's final reply is the post-tool-call text, not the raw tool_call markup",
          body.get("reply") == TURN2, body)

    tool_calls = body.get("tool_calls") or []
    check("exactly one tool call recorded", len(tool_calls) == 1, tool_calls)
    call = tool_calls[0] if tool_calls else {}
    check("the tool call was propose_graph", call.get("tool") == "propose_graph", call)
    check("the tool call's args carry the exact nodes the model proposed",
          call.get("args", {}).get("nodes") == PROPOSED_NODES, call)

    check("tool_calls[0] carries a 'result' field (the wire contract the frontend depends on)",
          "result" in call and isinstance(call["result"], str), call)
    try:
        result = json.loads(call.get("result") or "{}")
    except (ValueError, TypeError):
        result = {}
    check("result parses as JSON with status=success", result.get("status") == "success", result)
    graph_id = result.get("graphId")
    check("result carries a real graphId", bool(graph_id), result)

    if graph_id:
        stored = workflow_graph_store.get_graph(graph_id)
        check("that graphId is a REAL, retrievable draft graph", stored is not None, graph_id)
        check("the draft's owner is the real principal", stored is not None and stored.owner == "reveal_tester", stored)
        check("the draft is still a draft (never auto-published)", stored is not None and stored.status == "draft", stored)

    # ── The stream endpoint must carry the same contract (frontend actually uses this one) ─
    stream_resp = client.post("/workflow-chat/stream", json={"message": "build me another one"})
    check("/workflow-chat/stream responds", stream_resp.status_code == 200, stream_resp.status_code)
    done_line = None
    for raw_line in stream_resp.text.split("\n\n"):
        if raw_line.startswith("data:"):
            frame = json.loads(raw_line[5:].strip())
            if frame.get("type") == "done":
                done_line = frame
    check("stream's done event carries tool_calls too", bool(done_line and done_line.get("tool_calls")), done_line)
    if done_line and done_line.get("tool_calls"):
        stream_call = done_line["tool_calls"][0]
        check("stream's tool_calls[0] ALSO carries a parseable result with a real graphId",
              json.loads(stream_call.get("result") or "{}").get("graphId") is not None, stream_call)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
