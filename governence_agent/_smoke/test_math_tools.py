"""Smoke test for the math/utility tools added so the chat LLM never has to do
exact arithmetic itself (calculate, compute_stats, percent_change) -- see
gateway/app.py's "Math / utility tools" section.

Covers: happy-path correctness against Python's own `statistics` module (not
hand-typed constants), the edge cases each tool is explicitly designed not to
silently mishandle (bad/non-finite input, div-by-zero, unsafe expressions,
oversized exponents, n=1 stdev, a zero-base percent change), and "well
registered" -- no manifest entry, so build_tool_specs must expose them to
EVERY grant (gated or not) and on BOTH chat surfaces, same as
submit_workflow_request/propose_graph.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import statistics
import sys
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "math-tools"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "math_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "math_password",
    "GOVERNANCE_SESSION_SECRET": "math-tools-secret",
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
        print(f"  FAIL {name}", detail if detail is not None else "")


sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))
gateway_app = importlib.import_module("app")
import asyncio  # noqa: E402
import orchestrator  # noqa: E402
from mcp_server import mcp  # noqa: E402
from policy import manifest  # noqa: E402


async def call(name, **args):
    result = await mcp.call_tool(name, {**args, "session_id": "math-tools-test"})
    content = result[0] if isinstance(result, tuple) else result
    for part in content or []:
        if getattr(part, "text", None) is not None:
            return json.loads(part.text)
    return {}


class _NoGrant:
    """The most restrictive real grant shape: no all_tools, no backends at all."""
    all_tools = False
    tools_by_backend = {}


with TestClient(gateway_app.app, base_url="http://testserver"):

    # ── compute_stats ───────────────────────────────────────────────────────
    nums = [10.0, 20.0, 30.0, 40.0]
    r = asyncio.run(call("compute_stats", values=nums))
    check("compute_stats: sum", r.get("sum") == sum(nums), r)
    check("compute_stats: mean", r.get("mean") == statistics.mean(nums), r)
    check("compute_stats: min/max/range", (r.get("min"), r.get("max"), r.get("range")) == (10.0, 40.0, 30.0), r)
    check("compute_stats: median", r.get("median") == statistics.median(nums), r)
    check("compute_stats: sample stdev matches statistics.stdev",
          abs(r.get("sampleStdev") - statistics.stdev(nums)) < 1e-9, r)
    check("compute_stats: population stdev matches statistics.pstdev and differs from sample",
          abs(r.get("populationStdev") - statistics.pstdev(nums)) < 1e-9 and r.get("populationStdev") != r.get("sampleStdev"), r)
    check("compute_stats: sample/population variance also returned",
          abs(r.get("sampleVariance") - statistics.variance(nums)) < 1e-9
          and abs(r.get("populationVariance") - statistics.pvariance(nums)) < 1e-9, r)

    r_empty = asyncio.run(call("compute_stats", values=[]))
    check("compute_stats: empty input reported, not a crash or a fake zero", r_empty.get("status") == "empty_input", r_empty)

    r_bad = asyncio.run(call("compute_stats", values=[10, "not-a-number", 30]))
    check("compute_stats: non-numeric value reported explicitly, not silently dropped/zeroed",
          r_bad.get("status") == "invalid_input" and "not-a-number" in str(r_bad.get("invalidValues")), r_bad)

    r_inf = asyncio.run(call("compute_stats", values=[1, float("inf"), 3]))
    check("compute_stats: inf is rejected rather than silently poisoning the result", r_inf.get("status") == "invalid_input", r_inf)

    r_nan = asyncio.run(call("compute_stats", values=[1, float("nan"), 3]))
    check("compute_stats: nan is rejected the same way", r_nan.get("status") == "invalid_input", r_nan)

    r_one = asyncio.run(call("compute_stats", values=[42]))
    check("compute_stats: n=1 sample stdev is null, not a crash", r_one.get("status") == "ok" and r_one.get("sampleStdev") is None, r_one)
    check("compute_stats: n=1 population stdev is 0", r_one.get("populationStdev") == 0.0, r_one)

    # ── calculate ───────────────────────────────────────────────────────────
    expected = (45231.12 - 38004.50) / 38004.50
    r = asyncio.run(call("calculate", expression="(45231.12 - 38004.50) / 38004.50"))
    check("calculate: correct margin-style ratio", abs(r.get("result") - expected) < 1e-9, (r, expected))

    r_div0 = asyncio.run(call("calculate", expression="5 / 0"))
    check("calculate: division by zero reported, not a crash",
          r_div0.get("status") == "error" and r_div0.get("errorCode") == "division_by_zero", r_div0)

    r_name = asyncio.run(call("calculate", expression="__import__('os').system('echo pwned')"))
    check("calculate: NO code execution -- a non-arithmetic expression is rejected outright",
          r_name.get("status") == "error" and r_name.get("errorCode") == "invalid_expression", r_name)

    r_pow = asyncio.run(call("calculate", expression="2 ** 999999"))
    check("calculate: an oversized exponent is rejected rather than hanging on a huge computation",
          r_pow.get("status") == "error", r_pow)

    r_long = asyncio.run(call("calculate", expression="1+" * 150 + "1"))
    check("calculate: an overlong expression is rejected",
          r_long.get("status") == "error" and r_long.get("errorCode") == "expression_too_long", r_long)

    r_complex = asyncio.run(call("calculate", expression="(-8) ** (1/3)"))
    check("calculate: a non-real (complex) result is reported, not returned as-is", r_complex.get("status") == "error", r_complex)

    r_empty_expr = asyncio.run(call("calculate", expression="   "))
    check("calculate: blank expression reported, not a crash", r_empty_expr.get("status") == "empty_input", r_empty_expr)

    # ── percent_change ──────────────────────────────────────────────────────
    r = asyncio.run(call("percent_change", from_value=100.0, to_value=125.0))
    check("percent_change: 100 -> 125 is +25%", r.get("percentChange") == 25.0 and r.get("absoluteChange") == 25.0, r)

    r_decline = asyncio.run(call("percent_change", from_value=200.0, to_value=150.0))
    check("percent_change: a decline is negative, not accidentally flipped", r_decline.get("percentChange") == -25.0, r_decline)

    r_zero = asyncio.run(call("percent_change", from_value=0.0, to_value=50.0))
    check("percent_change: zero base reports undefined percent, not a ZeroDivisionError crash",
          r_zero.get("status") == "ok" and r_zero.get("percentChange") is None and r_zero.get("absoluteChange") == 50.0, r_zero)

    r_garbage = asyncio.run(call("percent_change", from_value="not-a-number", to_value=50.0))
    check("percent_change: non-numeric scalar reported cleanly, not an uncaught pydantic/ToolError crash",
          r_garbage.get("status") == "invalid_input", r_garbage)

    r_numeric_str = asyncio.run(call("percent_change", from_value="100", to_value="125"))
    check("percent_change: a numeric STRING (e.g. from Qwen's text tool-call parser) still works",
          r_numeric_str.get("status") == "ok" and r_numeric_str.get("percentChange") == 25.0, r_numeric_str)

    # ── group_stats ─────────────────────────────────────────────────────────
    orders = [
        {"group": "Open", "value": 100.0}, {"group": "Open", "value": 50.0},
        {"group": "Closed", "value": 200.0},
    ]
    r = asyncio.run(call("group_stats", rows=orders))
    check("group_stats: correct group count", r.get("groupCount") == 2, r)
    check("group_stats: Open bucket sum/count/mean",
          r.get("groups", {}).get("Open") == {"count": 2, "sum": 150.0, "mean": 75.0, "min": 50.0, "max": 100.0}, r)
    check("group_stats: Closed bucket is a singleton group",
          r.get("groups", {}).get("Closed") == {"count": 1, "sum": 200.0, "mean": 200.0, "min": 200.0, "max": 200.0}, r)
    check("group_stats: grand total spans every group", r.get("grandTotal") == 350.0, r)
    check("group_stats: rowCount reflects every input row", r.get("rowCount") == 3, r)

    r_empty = asyncio.run(call("group_stats", rows=[]))
    check("group_stats: empty input reported, not a crash", r_empty.get("status") == "empty_input", r_empty)

    r_malformed = asyncio.run(call("group_stats", rows=[{"group": "A", "value": 1}, {"group": "B"}, {"notgroup": 1, "value": 2}]))
    check("group_stats: malformed rows (missing keys) reported explicitly, not silently dropped",
          r_malformed.get("status") == "invalid_input" and len(r_malformed.get("invalidRows") or []) == 2, r_malformed)

    r_bad_value = asyncio.run(call("group_stats", rows=[{"group": "A", "value": "not-a-number"}]))
    check("group_stats: non-numeric value reported explicitly", r_bad_value.get("status") == "invalid_input", r_bad_value)

    r_numeric_group = asyncio.run(call("group_stats", rows=[{"group": 1, "value": 10.0}, {"group": 1, "value": 5.0}]))
    check("group_stats: a non-string group label (e.g. a status code) still buckets correctly",
          r_numeric_group.get("groups", {}).get("1") == {"count": 2, "sum": 15.0, "mean": 7.5, "min": 5.0, "max": 10.0}, r_numeric_group)

    # ── "well registered": no manifest entry, visible on every grant + surface ─
    MATH_TOOLS = {"compute_stats", "calculate", "percent_change", "group_stats"}

    for name in MATH_TOOLS:
        check(f"{name} has no manifest entry (a meta tool, not gated business data)", manifest.canonical(name) is None)

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    check("all math tools are registered on the gateway's MCP server", MATH_TOOLS <= names, names)

    specs = orchestrator.build_tool_specs(tools, _NoGrant())
    spec_names = {s["function"]["name"] for s in specs}
    check("all math tools survive grant-filtering even for the MOST restrictive grant (no all_tools, no backends)",
          MATH_TOOLS <= spec_names, spec_names)

    home_specs = orchestrator.build_tool_specs(tools, _NoGrant(), exclude=orchestrator.WORKFLOW_ONLY_TOOLS)
    home_names = {s["function"]["name"] for s in home_specs}
    check("all math tools are still visible to Home chat too (not accidentally workflow-only)",
          MATH_TOOLS <= home_names, home_names)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
