"""Offline test for the USE_LOCAL_LLM switch and the OpenAI<->Anthropic message
translation used by the Claude Sonnet backup backend -- no network.

Run:  python _smoke/test_anthropic_backend.py
"""
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "governance_core"))
sys.path.insert(0, str(_ROOT / "gateway"))

import orchestrator  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {name}")
    else:
        FAIL += 1; print(f"  FAIL {name}")


# 1) USE_LOCAL_LLM toggle
os.environ.pop("USE_LOCAL_LLM", None)
check("defaults to local when unset", orchestrator._use_local_llm() is True)
os.environ["USE_LOCAL_LLM"] = "false"
check("false disables local", orchestrator._use_local_llm() is False)
os.environ["USE_LOCAL_LLM"] = "TrUe"
check("true (any case) keeps local", orchestrator._use_local_llm() is True)
os.environ.pop("USE_LOCAL_LLM", None)

# 2) message translation: merges consecutive tool turns into one Anthropic
# user turn with multiple tool_result blocks (required for strict alternation)
messages = [
    {"role": "system", "content": "sys prompt"},
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "find_customer", "arguments": '{"q": "bob"}'}},
        {"id": "call_2", "type": "function", "function": {"name": "get_orders", "arguments": '{"id": 5}'}},
    ]},
    {"role": "tool", "tool_call_id": "call_1", "content": "result1"},
    {"role": "tool", "tool_call_id": "call_2", "content": "result2"},
    {"role": "assistant", "content": "final answer"},
]
system, out = orchestrator._messages_to_anthropic(messages)
print("message translation")
check("system prompt extracted", system == "sys prompt")
check("strict user/assistant alternation", [m["role"] for m in out] == ["user", "assistant", "user", "assistant"])
check("both tool_use blocks on the assistant turn", len(out[1]["content"]) == 2)
check("tool args parsed into dict input", out[1]["content"][0]["input"] == {"q": "bob"})
check("both tool_results merged into ONE user turn", len(out[2]["content"]) == 2)
check("tool_result content preserved", out[2]["content"][1] == {"type": "tool_result", "tool_use_id": "call_2", "content": "result2"})

# 3) tool spec translation (OpenAI function schema -> Anthropic input_schema)
specs = [{"type": "function", "function": {
    "name": "find_customer", "description": "desc",
    "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
}}]
atools = orchestrator._tools_to_anthropic(specs)
print("tool spec translation")
check("name/description preserved", atools[0]["name"] == "find_customer" and atools[0]["description"] == "desc")
check("parameters renamed to input_schema", atools[0]["input_schema"]["required"] == ["q"])
check("no tools -> None", orchestrator._tools_to_anthropic([]) is None)

# 4) unconfigured Anthropic backend -> None (graceful degrade, no exception)
os.environ["USE_LOCAL_LLM"] = "false"
for var in ("GOVERNANCE_ANTHROPIC_ENDPOINT", "GOVERNANCE_ANTHROPIC_API_KEY", "GOVERNANCE_ANTHROPIC_MODEL"):
    os.environ.pop(var, None)
print("unconfigured Anthropic backend")
check("default_llm_complete returns None", orchestrator.default_llm_complete() is None)
check("default_llm_stream returns None", orchestrator.default_llm_stream() is None)
check("not-configured message mentions Anthropic vars", "GOVERNANCE_ANTHROPIC" in orchestrator._not_configured_message())
os.environ.pop("USE_LOCAL_LLM", None)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
