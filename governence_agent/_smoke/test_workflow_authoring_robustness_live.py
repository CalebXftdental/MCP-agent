"""LIVE robustness sweep for the workflow-authoring copilot: the shipped
design (get_field_catalog + schema-aware build-mode gating + honest
too_large_for_context signal, plus the pagination redesign and mid-tool-call
repair from this session) against a broad set of real workflow-authoring
scenarios, driven by the real local Qwen model against the real ERP mirror --
not stubbed govern/parse, not a fake LLM.

Ported from this session's `robustness_sweep_workflow_authoring.py` (written
and validated during today's robustness sweep) into a permanent, following
`test_finance_bulk_tools.py`'s env-setup + mcp-minierp-subprocess pattern and
this suite's check()/sys.exit counter style, so this coverage survives past
the one-off investigation that produced it.

Covers: discovery-only questions (typed + untyped tools), small real reports
that should fully succeed, large real reports that should hit the honest
too-large guard, multi-tool chains, an AR-side bulk tool, a not-found case, a
no-tool-needed conceptual question, and a "build a complete report" ask
against an UNTYPED bulk tool.

Live and slow (real ERP + real local LLM, up to 10 tool-calling turns each
across 10 cases) -- same category as the other `_live.py` tests, not part of
the fast default suite. Requires gateway/.env.local's local_llm_api_key and a
reachable GOVERNANCE_CHAT_BASE_URL (see env setup below); skips cleanly if
either is missing.
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
TMP = ROOT / "_smoke" / ".tmp" / "robustness-sweep"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

PASS, FAIL = 0, 0


def check(name, cond, detail=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}", detail if detail is not None else "")


ENV_LOCAL_PATH = ROOT / "gateway" / ".env.local"
if not ENV_LOCAL_PATH.exists():
    print(f"SKIP: {ENV_LOCAL_PATH} not found -- no local LLM configured for this environment.")
    sys.exit(0)

ENV_LOCAL = ENV_LOCAL_PATH.read_text()


def _envval(key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}\s*=\s*(.+)$", ENV_LOCAL, flags=re.MULTILINE)
    return m.group(1).strip() if m else None


LOCAL_LLM_KEY = _envval("local_llm_api_key")
if not LOCAL_LLM_KEY:
    print("SKIP: local_llm_api_key not set in gateway/.env.local -- no local LLM configured for this environment.")
    sys.exit(0)

MINIERP_PORT = 18601

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "robustness_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "robustness_password",
    "GOVERNANCE_SESSION_SECRET": "robustness-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": f"http://127.0.0.1:{MINIERP_PORT}/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "60",
    "GOVERNANCE_CHAT_BASE_URL": "http://20.120.220.8:8000/v1",
    "GOVERNANCE_CHAT_MODEL": "/home/azureuser/models/Qwen3.6-27B-FP8",
    "GOVERNANCE_CHAT_API_KEY": LOCAL_LLM_KEY,
    "GOVERNANCE_CHAT_MAX_TOKENS": "1024",
    "USE_LOCAL_LLM": "true",
})


def wait_health(url: str, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.3)
    raise RuntimeError(f"service did not become healthy: {last}")


print("start mcp-minierp (real ERP mirror)")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
minierp = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(MINIERP_PORT), "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

try:
    wait_health(f"http://127.0.0.1:{MINIERP_PORT}/health")
    check("mcp-minierp (real ERP mirror) healthy", True)

    sys.path.insert(0, str(ROOT / "governance_core"))
    sys.path.insert(0, str(ROOT / "gateway"))

    from mcp_server import mcp  # noqa: E402
    from store import get_store  # noqa: E402
    from store.models import ConsumerRecord  # noqa: E402
    import request_context as ctx  # noqa: E402
    import orchestrator  # noqa: E402
    import llm_broker  # noqa: E402
    import app as gateway_app  # noqa: E402,F401  (side effect: registers gateway tools on `mcp`)

    record = ConsumerRecord(
        consumer_id="user:robustness", name="robustness", key_hash="",
        status="active", role="user", type="user", categories=["finance", "accounts", "orders"],
    )
    get_store().upsert_consumer(record)
    ctx.consumer_ctx.set(record.name)
    ctx.consumer_record_ctx.set(record)
    ctx.ip_ctx.set("test-harness")

    def make_logging_complete(real_adapted, log_list):
        async def wrapper(messages, tools, *, lane=llm_broker.BATCH, user="", **extra):
            t0 = time.perf_counter()
            error = None
            try:
                msg = await real_adapted(messages, tools, lane=lane, user=user, **extra)
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                msg = None
            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            log_list.append({
                "latency_ms": latency_ms, "error": error,
                "content": getattr(msg, "content", None) if msg else None,
                "tool_calls": [
                    {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                    for tc in (getattr(msg, "tool_calls", None) or [])
                ] if msg else [],
            })
            if error is not None:
                raise RuntimeError(error)
            return msg
        return wrapper

    local_adapted = orchestrator.default_llm_complete()
    if local_adapted is None:
        print("SKIP: local LLM did not come up configured (default_llm_complete() returned None).")
        sys.exit(0)

    _real_execute_tool = orchestrator.execute_tool

    async def shadow_execute_tool(mcp_, name, args, session_id, _sink):
        t0 = time.perf_counter()
        out = await _real_execute_tool(mcp_, name, args, session_id)
        _sink.append({"tool": name, "args": args, "result": out,
                      "latency_ms": round((time.perf_counter() - t0) * 1000, 1)})
        return out

    # ── Discovery: a real vendor code with due-soon AP invoices ─────────────
    async def discover():
        raw = await orchestrator.execute_tool(
            mcp, "minierp_finance_get_ap_invoices_due_soon",
            {"days_ahead": 365, "page": 1, "page_size": 50}, "discovery",
        )
        due_soon = json.loads(raw)
        invoices = due_soon.get("invoices") or []
        assert invoices, f"no AP invoices found for discovery: {due_soon}"
        return invoices[0]["vendorCode"]

    vendor_a = asyncio.run(discover())
    print(f"discovered vendor_a={vendor_a!r}")

    USE_CASES = {
        "UC1_discovery_typed": (
            "What fields does the AP-invoices-due-soon data actually have? Don't build anything, "
            "just tell me the fields."
        ),
        "UC2_discovery_untyped": (
            "What fields are returned when looking up customers by region? Don't build anything, "
            "just tell me the fields."
        ),
        "UC3_small_real_report": (
            "Build me a report of AP invoices due in the next 3 days."
        ),
        "UC4_large_real_report": (
            "Build me a report of every AP invoice due in the next 365 days."
        ),
        "UC5_vendor_chain": (
            f"Give me vendor {vendor_a}'s profile and every AP invoice on file for them."
        ),
        "UC6_ar_side_large_report": (
            "Build a report of AR invoices older than 300 days that may still be outstanding."
        ),
        "UC7_not_found_vendor": (
            "Build me a report of AP invoices for vendor ZZZ-DOES-NOT-EXIST-999."
        ),
        "UC8_no_tool_conceptual": (
            "In plain terms, without looking anything up, what's the difference between an AP "
            "invoice and an AR invoice?"
        ),
        "UC9_untyped_bulk_report": (
            "Build me a report of every customer in California."
        ),
        "UC10_typed_payment_history": (
            f"Show me the full payment history for vendor {vendor_a} -- every payment and which "
            "invoice it was applied to."
        ),
    }

    async def run_one(uc_name, message):
        log: list[dict] = []
        exec_log: list[dict] = []
        logging_complete = make_logging_complete(local_adapted, log)
        session_id = f"workflow-chat:robustness:{uc_name}"
        orchestrator.execute_tool = lambda m, n, a, s, _sink=exec_log: shadow_execute_tool(m, n, a, s, _sink)
        t0 = time.perf_counter()
        try:
            result = await orchestrator.run_chat(
                mcp, message, session_id, record,
                llm_complete=logging_complete, system_prompt=orchestrator.WORKFLOW_COPILOT_SYSTEM_PROMPT,
                exclude_tools=frozenset(), max_turns=orchestrator.WORKFLOW_CHAT_MAX_TURNS,
            )
        except Exception as exc:  # noqa: BLE001
            result = {"reply": f"<EXCEPTION: {exc}>", "tool_calls": list(exec_log), "crashed": True}
        finally:
            orchestrator.execute_tool = _real_execute_tool
        wall_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {"result": result, "turn_log": log, "wall_clock_ms": wall_ms}

    def classify(run):
        reply = run["result"].get("reply") or ""
        tool_calls = run["result"].get("tool_calls") or []
        statuses = []
        for tc in tool_calls:
            try:
                statuses.append(json.loads(tc["result"]).get("status"))
            except Exception:  # noqa: BLE001
                statuses.append(None)
        if run["result"].get("crashed") or reply.startswith("<EXCEPTION"):
            return "CRASH"
        if reply == "I wasn't able to complete that within the tool-call limit.":
            return "TURN_BUDGET_EXHAUSTED"
        if "too_large_for_context" in statuses:
            return "GRACEFUL_TOO_LARGE"
        if "<tool_call>" in reply or "<function=" in reply:
            return "GARBLED_OUTPUT"
        return "CLEAN"

    async def run_all():
        out = {}
        for uc_name, message in USE_CASES.items():
            print(f"\n=== {uc_name} ===")
            run = await run_one(uc_name, message)
            tools = [tc["tool"] for tc in run["result"].get("tool_calls", [])]
            verdict = classify(run)
            print(f"  verdict={verdict}  wall={run['wall_clock_ms']:.0f}ms  turns={len(run['turn_log'])}  tools={tools}")
            print("  reply:", (run["result"].get("reply") or "")[:220].replace("\n", " "))
            out[uc_name] = {"run": run, "verdict": verdict, "tools": tools}
        return out

    all_results = asyncio.run(run_all())

    with open(TMP / "results.json", "w") as f:
        json.dump(all_results, f, indent=2, default=str)

    print("\n" + "=" * 78)
    print("ROBUSTNESS SWEEP ASSERTIONS")
    print("=" * 78)

    _BAD_VERDICTS = {"CRASH", "TURN_BUDGET_EXHAUSTED", "GARBLED_OUTPUT"}
    for uc_name, r in all_results.items():
        check(
            f"{uc_name}: verdict is not {sorted(_BAD_VERDICTS)} (got {r['verdict']})",
            r["verdict"] not in _BAD_VERDICTS,
            {"verdict": r["verdict"], "reply": r["run"]["result"].get("reply"), "tools": r["tools"]},
        )

    # A few cases get an additional structural check, on top of the blanket
    # non-crash/non-exhausted/non-garbled assertion above.
    uc1_tools = all_results["UC1_discovery_typed"]["tools"]
    check(
        "UC1 (discovery on a TYPED tool): used get_field_catalog rather than a real data call",
        "get_field_catalog" in uc1_tools, uc1_tools,
    )
    uc8_tools = all_results["UC8_no_tool_conceptual"]["tools"]
    check(
        "UC8 (pure conceptual question, no lookup needed): made no tool calls at all",
        uc8_tools == [], uc8_tools,
    )

    print("\n--- TALLY ---")
    counts: dict[str, int] = {}
    for r in all_results.values():
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    for verdict, n in sorted(counts.items()):
        print(f"  {verdict}: {n}")
    print(f"\nfull results written to {TMP / 'results.json'}")

finally:
    minierp.terminate()
    try:
        minierp.wait(timeout=10)
    except subprocess.TimeoutExpired:
        minierp.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
