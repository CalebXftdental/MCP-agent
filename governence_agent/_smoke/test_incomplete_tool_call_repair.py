"""Smoke test: a malformed/truncated tool-call turn (an opening <tool_call>/
<function=...> tag with no clean matching close -- e.g. generation cut off
mid tool-call) must trigger one corrective retry instead of surfacing the
raw garbled fragment as the reply, and a second failure in a row must fall
through to a clear, honest message rather than looping or ever leaking raw
XML to the user.

Offline: no servers, no LLM, no network -- same FakeMCP/Delta pattern as
test_streaming.py / test_tool_call_resilience.py.
"""
from __future__ import annotations

import asyncio
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


class FakeMCP:
    async def list_tools(self):
        return []

    async def call_tool(self, name, args):
        raise AssertionError("no tool call should ever execute in this test")


_INCOMPLETE = '<tool_call><function=minierp_orders_get_order_details><parameter=order_number>SO-1'


# ── non-streaming: run_chat, retries once then succeeds ─────────────────────

def incomplete_then_clean():
    state = {"n": 0, "messages": []}

    def complete(messages, tools):
        state["n"] += 1
        state["messages"].append(list(messages))
        if state["n"] == 1:
            return Delta(content=_INCOMPLETE)
        return Delta(content="Order SO-1 is Open.")
    return complete, state


complete_fn, state = incomplete_then_clean()
result = asyncio.run(orchestrator.run_chat(FakeMCP(), "status of SO-1", "s1", None, llm_complete=complete_fn))
print("run_chat: one retry recovers from a truncated tool-call")
check("exactly two LLM calls were made (original + one retry)", state["n"] == 2, state["n"])
check("the clean retry answer was returned, not the garbled fragment",
      result.get("reply") == "Order SO-1 is Open.", result)
check("no tool calls were recorded (nothing ever parsed as complete)", result.get("tool_calls") == [], result)
nudge_seen = any(
    m[-1].get("role") == "user" and "cut off mid tool-call" in (m[-1].get("content") or "")
    for m in state["messages"][1:]
)
check("the retry turn included the corrective nudge", nudge_seen, state["messages"])


# ── non-streaming: run_chat, fails twice in a row -> honest giveup message ──

def incomplete_twice():
    state = {"n": 0}

    def complete(messages, tools):
        state["n"] += 1
        return Delta(content=_INCOMPLETE)
    return complete, state


complete_fn2, state2 = incomplete_twice()
result2 = asyncio.run(orchestrator.run_chat(FakeMCP(), "status of SO-1", "s2", None, llm_complete=complete_fn2))
print("run_chat: two failures in a row give up cleanly")
check("exactly two LLM calls were made (no further looping)", state2["n"] == 2, state2["n"])
check("a clear, honest give-up message was returned",
      result2.get("reply") == orchestrator._INCOMPLETE_TOOLCALL_GIVEUP_MSG, result2)
check("raw tool-call markup never leaked to the reply", "<tool_call" not in result2.get("reply", ""), result2)


# ── streaming: run_chat_stream, retries once then succeeds ──────────────────

def incomplete_then_clean_stream():
    state = {"n": 0}

    def stream(messages, tools):
        state["n"] += 1
        if state["n"] == 1:
            yield Delta(content=_INCOMPLETE)
        else:
            for tok in ["Order ", "SO-1 is ", "Open."]:
                yield Delta(content=tok)
    return stream, state


async def collect(agen):
    return [ev async for ev in agen]


stream_fn, state3 = incomplete_then_clean_stream()
evs = asyncio.run(collect(orchestrator.run_chat_stream(FakeMCP(), "status of SO-1", "s3", None, llm_stream=stream_fn)))
print("run_chat_stream: one retry recovers from a truncated tool-call")
check("exactly two stream calls were made (original + one retry)", state3["n"] == 2, state3["n"])
final_text = "".join(e["text"] for e in evs if e["type"] == "delta")
check("the clean retry answer streamed, not the garbled fragment", final_text == "Order SO-1 is Open.", evs)
check("raw tool-call markup never leaked to the client", all("<tool_call" not in e.get("text", "") for e in evs), evs)
check("stream ends with done", evs[-1]["type"] == "done", evs)


# ── streaming: run_chat_stream, fails twice in a row -> honest giveup ───────

def incomplete_twice_stream():
    state = {"n": 0}

    def stream(messages, tools):
        state["n"] += 1
        yield Delta(content=_INCOMPLETE)
    return stream, state


stream_fn2, state4 = incomplete_twice_stream()
evs2 = asyncio.run(collect(orchestrator.run_chat_stream(FakeMCP(), "status of SO-1", "s4", None, llm_stream=stream_fn2)))
print("run_chat_stream: two failures in a row give up cleanly")
check("exactly two stream calls were made (no further looping)", state4["n"] == 2, state4["n"])
giveup_text = "".join(e["text"] for e in evs2 if e["type"] in ("delta", "replace"))
check("a clear, honest give-up message was streamed",
      giveup_text == orchestrator._INCOMPLETE_TOOLCALL_GIVEUP_MSG, evs2)
check("raw tool-call markup never leaked to the client", "<tool_call" not in giveup_text, giveup_text)
check("stream ends with done", evs2[-1]["type"] == "done", evs2)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
