"""Smoke test: a tool call that raises (bad args past FastMCP's own pydantic
validation, a backend timeout, any unhandled error) must NOT crash the whole
chat turn. Before this fix, orchestrator.execute_tool had no try/except around
mcp.call_tool, and neither run_chat nor run_chat_stream wraps its own call
sites -- one bad tool call took the whole turn down instead of surfacing a
message the model could see, explain to the user, or retry from.

Offline: no servers, no LLM, no network -- same FakeMCP/Delta pattern as
test_streaming.py.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "governance_core"))
sys.path.insert(0, str(_ROOT / "gateway"))

import orchestrator  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}", detail if detail is not None else "")


class Delta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeMCPRaising:
    """A backend whose tool call always raises -- e.g. a pydantic ValidationError
    from bad LLM-supplied arguments, or a transport failure to the real backend."""
    async def list_tools(self):
        return []

    async def call_tool(self, name, args):
        raise RuntimeError("simulated backend failure (e.g. a validation error or timeout)")


# ── non-streaming: run_chat ─────────────────────────────────────────────────

def failing_tool_then_answer():
    state = {"n": 0}

    def complete(messages, tools):
        state["n"] += 1
        if state["n"] == 1:
            return Delta(content='<tool_call><function=minierp_orders_get_order_details>'
                                  '<parameter=order_number>SO-1</parameter></function></tool_call>')
        return Delta(content="I couldn't complete that lookup.")
    return complete, state


complete_fn, state = failing_tool_then_answer()
result = asyncio.run(orchestrator.run_chat(FakeMCPRaising(), "status of SO-1", "s1", None, llm_complete=complete_fn))
print("run_chat: a raising tool call does not crash the turn")
check("run_chat completed instead of raising out of the test", True)  # implicit: asyncio.run above didn't raise
check("the turn still reached a final answer", result.get("reply") == "I couldn't complete that lookup.", result)
check("exactly one tool call was recorded", len(result.get("tool_calls") or []) == 1, result)
tool_result = json.loads(result["tool_calls"][0]["result"])
check("the failure surfaced as a governance/error result, not a raw traceback",
      tool_result.get("source") == "governance" and tool_result.get("status") == "error", tool_result)
check("the error names which tool failed", tool_result.get("tool") == "minierp_orders_get_order_details", tool_result)
check("the model saw enough to react (a message mentioning the failure)",
      "failed" in (tool_result.get("message") or "").lower(), tool_result)


# ── streaming: run_chat_stream ──────────────────────────────────────────────

def failing_tool_then_stream():
    state = {"n": 0}

    def stream(messages, tools):
        state["n"] += 1
        if state["n"] == 1:
            yield Delta(content='<tool_call><function=minierp_orders_get_order_details>'
                                 '<parameter=order_number>SO-1</parameter></function></tool_call>')
        else:
            for tok in ["That lookup ", "didn't go through."]:
                yield Delta(content=tok)
    return stream


async def collect(agen):
    return [ev async for ev in agen]


evs = asyncio.run(collect(orchestrator.run_chat_stream(FakeMCPRaising(), "status of SO-1", "s2", None,
                                                        llm_stream=failing_tool_then_stream())))
print("run_chat_stream: a raising tool call does not crash the turn")
check("the stream still ran to completion", evs and evs[-1]["type"] == "done", evs)
tools_ev = next((e for e in evs if e["type"] == "tools"), None)
check("a tools event was still emitted for the attempted call", tools_ev is not None, evs)
final_text = "".join(e["text"] for e in evs if e["type"] == "delta")
check("the final answer still streamed after the failed tool call", final_text == "That lookup didn't go through.", final_text)
done_ev = evs[-1]
stream_tool_result = json.loads(done_ev["tool_calls"][0]["result"])
check("streaming path reports the same clean error shape as non-streaming",
      stream_tool_result.get("source") == "governance" and stream_tool_result.get("status") == "error", stream_tool_result)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
