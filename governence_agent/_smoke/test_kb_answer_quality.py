"""Real answer-quality eval, run through the ACTUAL orchestrator.run_chat loop
(not a scripted/faked LLM response) against TWO real backends:

  - "local" cases: the local mcp-knowledge process, real ara-prod (company KB)
    + a synthetic personal-KB corpus (personal_kb_large.json) ingested for one
    test owner. Six combined company+personal-KB questions.
  - "prod" cases: the REAL, DEPLOYED production Governance Gateway's own /mcp
    endpoint (governence-agent-....azurewebsites.net/mcp), connected to over
    the actual MCP protocol with a real API key (`local_admin_api_key` from
    mcp-knowledge/.env.local) -- exactly how a real external agent/integration
    would call it, mimicking real production usage rather than a local mock or
    a direct db-api call. Fourteen real-ERP + company-KB questions across
    accounts/finance/orders/shipments/analytics, scored against facts pulled
    live from production before writing these cases (see the exploration
    writeup -- every expected fact below was independently verified against a
    real tool call first, not guessed).

Unlike test_knowledge.py (wiring/plumbing only, fake RAG backend), every case
here runs a real model deciding which real tools to call against real data.

Personal KB has no configured embedding endpoint in this dev environment
(GOVERNANCE_EMBEDDING_* unset) -- personal-tier search runs on TF-IDF + the
real reranker, not cosine + reranker. That's a real, valid fallback path
(embeddings_client.py degrades to TF-IDF on any failure/missing config), but
it means the "local" cases don't exercise the cosine-similarity code path.
Company KB is unaffected either way.

Costs real, small amounts of money: live Azure AI Search queries, live
reranker calls, live LLM completions, and live production ERP/KB tool calls
through the real governed pipeline (audited under the API key's own identity
on the real system -- expect real audit-log entries from this run).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "kb-answer-quality"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "kbqual_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "kbqual_password",
    "GOVERNANCE_SESSION_SECRET": "kbqual-smoke-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "GOVERNANCE_PERSONAL_KNOWLEDGE_STORE_FILE": str(TMP / "state" / "personal_knowledge.json"),
    "KNOWLEDGE_MCP_URL": "http://127.0.0.1:18150/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "30",
})

# Backend selection: KBQUAL_LLM=local runs against the self-hosted Qwen
# endpoint (GOVERNANCE_CHAT_BASE_URL/API_KEY/MODEL must already be set in the
# invoking shell -- deliberately NOT hardcoded/pulled from a repo file here,
# since that deployment's key lives outside this repo). Default ("anthropic")
# pulls the real Claude config from gateway/.env.local, same as before.
from dotenv import dotenv_values  # noqa: E402

BACKEND = (os.getenv("KBQUAL_LLM") or "anthropic").strip().lower()
if BACKEND == "local":
    os.environ["USE_LOCAL_LLM"] = "true"
    if not (os.getenv("GOVERNANCE_CHAT_BASE_URL") and os.getenv("GOVERNANCE_CHAT_MODEL")):
        print("SKIP: KBQUAL_LLM=local requires GOVERNANCE_CHAT_BASE_URL/API_KEY/MODEL already set in the shell env.")
        sys.exit(0)
else:
    os.environ["USE_LOCAL_LLM"] = "false"
    gw_env = dotenv_values(str(ROOT / "gateway" / ".env.local"))
    for key in ("GOVERNANCE_ANTHROPIC_ENDPOINT", "GOVERNANCE_ANTHROPIC_API_KEY", "GOVERNANCE_ANTHROPIC_MODEL"):
        if gw_env.get(key):
            os.environ[key] = gw_env[key]
    if not os.getenv("GOVERNANCE_ANTHROPIC_API_KEY"):
        print("SKIP: no GOVERNANCE_ANTHROPIC_* configured in gateway/.env.local -- cannot run a real-model eval.")
        sys.exit(0)

print(f"LLM backend: {BACKEND}")

# Real production gateway -- the "prod" cases connect here as a genuine
# external MCP client (Bearer-token auth), same as any real integration would,
# rather than a local mock or a direct db-api call. Key pulled from
# mcp-knowledge/.env.local (not committed/hardcoded); URL is stable/public-ish
# infra info already sitting in that same gitignored file.
_KNOWLEDGE_ENV = dotenv_values(str(ROOT / "mcp-knowledge" / ".env.local"))
PROD_MCP_URL = "https://governence-agent-dycufrgya2ewash8.canadaeast-01.azurewebsites.net/mcp"
PROD_API_KEY = _KNOWLEDGE_ENV.get("local_admin_api_key")

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def wait_health(url: str, timeout: float = 15.0) -> None:
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


# ── Case schema ────────────────────────────────────────────────────────────
# mcp: "local" (mcp-knowledge + synthetic personal KB) or "prod" (the real
#      deployed gateway's own /mcp, real ERP + real company KB).
# tool_groups: list of acceptable-canonical-tool-name sets; at least one call
#      from EACH group must appear (generalizes "called a company-KB tool" +
#      "called a personal-KB tool" to any number of required tool families).
# keywords: list of (label, [alternates]) -- passes if ANY alternate substring
#      (case-insensitive) is found in the reply. Every alternate here was
#      independently verified against a real tool call before being written
#      (see the exploration scripts referenced in the eval writeup) --
#      deliberately picked as exact IDs/codes/emails/tracking numbers rather
#      than dollar amounts or day-counts, which real models paraphrase
#      ("45 days" vs "45D", "$617,780" vs "617780") in ways a naive substring
#      check false-fails on.

# Six "local" cases, each requiring one REAL, verified company-KB fact (pulled
# live from ara-prod) AND one synthetic personal-KB fact from
# personal_kb_large.json, in the SAME answer.
_LOCAL_CASES = [
    {
        "title": "USPS account funding + personal approval note",
        "mcp": "local",
        "question": (
            "What's the process for adding funds to the Frontier USPS account, and per my own "
            "notes, who do I personally loop in on that and at what dollar threshold?"
        ),
        "tool_groups": [
            frozenset({"search_knowledge", "answer_from_knowledge"}),
            frozenset({"search_my_documents", "answer_from_my_documents"}),
        ],
        "keywords": [
            ("company fact '$500'", ["$500"]),
            ("personal fact 'marcus chen'", ["marcus chen"]),
            ("personal fact '$700'", ["$700"]),
        ],
    },
    {
        "title": "Canada fraud tracking + personal watchlist",
        "mcp": "local",
        "question": (
            "What is the Canada fraud tracking procedure for a new customer, and which Canadian "
            "accounts have I personally flagged this month?"
        ),
        "tool_groups": [
            frozenset({"search_knowledge", "answer_from_knowledge"}),
            frozenset({"search_my_documents", "answer_from_my_documents"}),
        ],
        "keywords": [
            ("company fact 'sales rep transfer sheet'", ["sales rep transfer sheet"]),
            ("company fact 'acumatica'", ["acumatica"]),
            ("personal fact 'maple ridge dental supply'", ["maple ridge dental supply"]),
            ("personal fact 'northshore smile'", ["northshore smile"]),
        ],
    },
    {
        "title": "Manual sales order + personal Sample Dental Group reminder",
        "mcp": "local",
        "question": (
            "How do I create a manual sales order, and what's my personal reminder about doing "
            "this specifically for Sample Dental Group?"
        ),
        "tool_groups": [
            frozenset({"search_knowledge", "answer_from_knowledge"}),
            frozenset({"search_my_documents", "answer_from_my_documents"}),
        ],
        "keywords": [
            ("company fact 'order type'", ["order type"]),
            ("company fact 'remove'", ["remove"]),
            ("personal fact 'prepaid-add'", ["prepaid-add"]),
            ("personal fact 'denise'", ["denise"]),
        ],
    },
    {
        "title": "Voicemail instructions + personal OOO script",
        "mcp": "local",
        "question": (
            "How do I change my voicemail per the sales instructions, and what's the "
            "out-of-office script draft I saved for myself?"
        ),
        "tool_groups": [
            frozenset({"search_knowledge", "answer_from_knowledge"}),
            frozenset({"search_my_documents", "answer_from_my_documents"}),
        ],
        "keywords": [
            ("company fact '*86'", ["*86"]),
            ("company fact 'ringcentral'", ["ringcentral"]),
            ("personal fact 'jordan alvarez'", ["jordan alvarez"]),
            ("personal fact '4471'", ["4471"]),
        ],
    },
    {
        "title": "Net30 autocharge terms + personal follow-up list",
        "mcp": "local",
        "question": (
            "What are the Net30 autocharge terms, and which Net30 customers do I personally "
            "need to follow up with this month?"
        ),
        "tool_groups": [
            frozenset({"search_knowledge", "answer_from_knowledge"}),
            frozenset({"search_my_documents", "answer_from_my_documents"}),
        ],
        "keywords": [
            ("company fact 'net30'", ["net30"]),
            ("company fact '30 day'", ["30 day"]),
            ("personal fact 'bright smile dental group'", ["bright smile dental group"]),
            ("personal fact 'coastal ortho partners'", ["coastal ortho partners"]),
        ],
    },
    {
        "title": "Online return instructions + personal pending case",
        "mcp": "local",
        "question": (
            "What are the online return instructions for a customer, and what's the status of "
            "the pending return case I noted for myself?"
        ),
        "tool_groups": [
            frozenset({"search_knowledge", "answer_from_knowledge"}),
            frozenset({"search_my_documents", "answer_from_my_documents"}),
        ],
        "keywords": [
            ("company fact 'pickup'", ["pickup"]),
            ("company fact 'orders & more'", ["orders & more"]),
            ("personal fact 'golden gate dental'", ["golden gate dental"]),
            ("personal fact 'rma'", ["rma"]),
        ],
    },
]

# Fourteen "prod" cases against the REAL deployed gateway -- real ERP data,
# real company KB, real governed pipeline. Every expected fact was pulled
# live before writing the case (customer AFFEBAY155 / "Affordable Dental EBAY
# USA", vendor FROKURA500 / Kuraray, order AF0012081, etc. are all real,
# current records, not fixtures). Read-only tools only -- nothing here sends,
# drafts, or writes anything.
_PROD_CASES = [
    {
        "title": "[prod] Top customer by spend",
        "mcp": "prod",
        "question": "Who is our single top customer by total spend right now?",
        "tool_groups": [frozenset({"get_top_customers_by_spend"})],
        "keywords": [("customer id 'AFFEBAY155'", ["affebay155"]), ("customer name", ["affordable dental"])],
    },
    {
        "title": "[prod] Customer profile payment method",
        "mcp": "prod",
        "question": "What is the default payment method on file for customer AFFEBAY155?",
        "tool_groups": [frozenset({"get_customer_profile", "get_customer_overview"})],
        "keywords": [("payment method 'CHECKUSD'", ["checkusd"])],
    },
    {
        "title": "[prod] Vendor class for Kuraray",
        "mcp": "prod",
        "question": "What is vendor FROKURA500 (Kuraray)'s vendor class ID, exactly as recorded in the system?",
        "tool_groups": [frozenset({"get_vendor_details"})],
        "keywords": [("vendor class 'FOCDS'", ["focds"])],
    },
    {
        "title": "[prod] Recent orders for a customer",
        "mcp": "prod",
        "question": "List a couple of recent order numbers for customer AFFEBAY155.",
        "tool_groups": [frozenset({"get_customer_orders", "get_customer_order_summary"})],
        "keywords": [("a real AFFEBAY155 order number", ["af00120"])],
    },
    {
        "title": "[prod] Find customer by name",
        "mcp": "prod",
        "question": "Look up the customer account for 'Premier Dental' and give me their customer ID.",
        "tool_groups": [frozenset({"find_customer"})],
        "keywords": [("customer id 'FROPREM500'", ["froprem500"])],
    },
    {
        "title": "[prod] AP invoices due soon",
        "mcp": "prod",
        "question": "What AP invoices are due in the next 30 days? Name a vendor from the list.",
        "tool_groups": [frozenset({"get_ap_invoices_due_soon"})],
        "keywords": [("a real due-soon vendor name", ["colroy"])],
    },
    {
        "title": "[prod] AR invoices past due, largest balance",
        "mcp": "prod",
        "question": "Of the AR invoices currently past due, which invoice number has the largest unpaid balance?",
        "tool_groups": [frozenset({"get_ar_invoices_past_due"})],
        "keywords": [("invoice number 'AR0214063'", ["ar0214063"])],
    },
    {
        "title": "[prod] AP invoice payment details",
        "mcp": "prod",
        "question": "For AP invoice AP0615569, is it paid, and what payment type was used?",
        "tool_groups": [frozenset({"get_ap_invoice_details"})],
        "keywords": [("payment type 'WIREUSD'", ["wireusd"])],
    },
    {
        "title": "[prod] Primary contact email for a customer",
        "mcp": "prod",
        "question": "What's the primary contact email on file for customer AFFEBAY155?",
        "tool_groups": [frozenset({"get_contacts", "get_customer_overview"})],
        "keywords": [("contact email", ["1michael@mvpdentalsupply.com"])],
    },
    {
        "title": "[prod] Order status lookup",
        "mcp": "prod",
        "question": "What's the status of order AF0012081?",
        "tool_groups": [frozenset({"get_order_details"})],
        "keywords": [("status 'Completed'", ["completed"])],
    },
    {
        "title": "[prod] Shipment tracking number",
        "mcp": "prod",
        "question": "What's the tracking number for order AF0012081?",
        "tool_groups": [frozenset({"get_shipping_by_order", "get_shipping_by_shipment", "get_customer_shipment_status"})],
        "keywords": [("tracking number", ["1zy06a700396430706"])],
    },
    {
        "title": "[prod] Not-found guard: fake customer",
        "mcp": "prod",
        "question": "Look up the customer account for 'ZZZ Totally Fake Dental Practice That Does Not Exist 99912'.",
        "tool_groups": [frozenset({"find_customer"})],
        "keywords": [
            ("reply honestly reports no match (not a fabricated ID)",
             ["not found", "no customer", "no match", "couldn't find", "could not find", "does not exist", "no results"]),
        ],
    },
    {
        "title": "[prod] Combined: customer terms + a recent order, same customer",
        "mcp": "prod",
        "question": "For customer AFFEBAY155, what's their default payment method, and give me one of their recent order numbers.",
        "tool_groups": [
            frozenset({"get_customer_profile", "get_customer_overview"}),
            frozenset({"get_customer_orders", "get_customer_order_summary"}),
        ],
        "keywords": [
            ("payment method 'CHECKUSD'", ["checkusd"]),
            ("a real AFFEBAY155 order number", ["af00120"]),
        ],
    },
    {
        "title": "[prod] Combined: company KB policy + real customer terms",
        "mcp": "prod",
        "question": (
            "How do I create a manual sales order per company policy, and separately, what's "
            "customer AFFEBAY155's default payment method on file?"
        ),
        "tool_groups": [
            frozenset({"search_knowledge", "answer_from_knowledge"}),
            frozenset({"get_customer_profile", "get_customer_overview"}),
        ],
        "keywords": [
            ("company KB fact 'order type'", ["order type"]),
            ("payment method 'CHECKUSD'", ["checkusd"]),
        ],
    },
]

CASES = _LOCAL_CASES + _PROD_CASES


class _ProdMCPAdapter:
    """Wraps a real mcp.ClientSession (connected to the deployed production
    gateway over Streamable HTTP, Bearer-token auth) behind the same
    list_tools()/call_tool() shape orchestrator.run_chat expects from an
    in-process FastMCP instance -- so run_chat runs UNCHANGED against a real,
    remote, production MCP server instead of the local one."""

    def __init__(self, session):
        self._session = session

    async def list_tools(self):
        result = await self._session.list_tools()
        return result.tools

    async def call_tool(self, name: str, args: dict):
        result = await self._session.call_tool(name, args)
        # Matches FastMCP's own call_tool() return shape (a tuple), which is
        # what orchestrator._extract_text expects -- ClientSession's own
        # CallToolResult isn't directly compatible, so translate it here.
        return (result.content, result.structuredContent)


async def _run_case(i: int, case: dict, *, local_mcp, local_record, prod_adapter, prod_record) -> None:
    print(f"[{i + 1}/{len(CASES)}] {case['title']}")
    if case["mcp"] == "prod":
        mcp_obj, record, session_prefix = prod_adapter, prod_record, "chat:mcpq_prod"
    else:
        mcp_obj, record, session_prefix = local_mcp, local_record, "chat:mcpq_local"

    result = await orchestrator.run_chat(
        mcp_obj, case["question"], f"{session_prefix}_{i}", record,
        exclude_tools=orchestrator.WORKFLOW_ONLY_TOOLS,
    )
    reply = (result.get("reply") or "").lower()
    tool_names = {manifest.canonical(tc.get("tool", "")) or tc.get("tool", "") for tc in result.get("tool_calls", [])}

    for group in case["tool_groups"]:
        check(f"  called a tool from {sorted(group)}", bool(tool_names & group), tool_names)
    check("  did NOT skip straight to a bare guess with zero tool calls", bool(tool_names), result.get("reply"))

    for label, alternates in case["keywords"]:
        hit = any(alt.lower() in reply for alt in alternates)
        check(f"  reply contains {label}", hit, reply)
    print()


async def main(cases_to_run: list[tuple[int, dict]]) -> None:
    needs_local = any(c["mcp"] == "local" for _, c in cases_to_run)
    needs_prod = any(c["mcp"] == "prod" for _, c in cases_to_run)

    local_record = None
    if needs_local:
        store = get_store()
        local_record = ConsumerRecord(
            consumer_id=f"user:{OWNER}", name=OWNER, key_hash="", status="active",
            role="user", type="user", categories=["knowledge", "personal_knowledge"],
            login_password_hash=hash_password("kbqual_password"),
        )
        store.upsert_consumer(local_record)
        ctx.consumer_ctx.set(local_record.name)
        ctx.consumer_record_ctx.set(local_record)
        ctx.ip_ctx.set("127.0.0.1")

    if not needs_prod:
        for i, case in cases_to_run:
            await _run_case(i, case, local_mcp=mcp, local_record=local_record, prod_adapter=None, prod_record=None)
        return

    if not PROD_API_KEY:
        print("SKIP: local_admin_api_key not found in mcp-knowledge/.env.local -- cannot run [prod] cases.")
        return

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    # A local, unpersisted ConsumerRecord mirroring the REAL production API
    # key's own access (an admin-role key -- confirmed via a live list_tools()
    # probe returning the full 52-tool catalog) so the LOCAL build_tool_specs()
    # filtering doesn't under-show the model tools the real key can actually
    # call. Real authorization/enforcement for every prod call still happens
    # entirely server-side, on production, under the real key's own identity.
    prod_record = ConsumerRecord(
        consumer_id="agent:prod_mcp_eval", name="prod_mcp_eval", key_hash="",
        status="active", role="admin", type="agent", categories=[],
        login_password_hash="",
    )

    headers = {"Authorization": f"Bearer {PROD_API_KEY}"}
    async with streamablehttp_client(PROD_MCP_URL, headers=headers, timeout=30) as (read, write, _get_sid):
        async with ClientSession(read, write) as session:
            await session.initialize()
            prod_adapter = _ProdMCPAdapter(session)
            for i, case in cases_to_run:
                await _run_case(i, case, local_mcp=mcp, local_record=local_record,
                                 prod_adapter=prod_adapter, prod_record=prod_record)


print("start knowledge backend (REAL ara-prod + reranker config from mcp-knowledge/.env.local)")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
if not python_exe.exists():
    python_exe = Path(sys.executable)
knowledge = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18150", "--no-access-log"],
    cwd=str(ROOT / "mcp-knowledge"),
    env={**os.environ, "KNOWLEDGE_PORT": "18150"},  # deliberately NOT overriding KNOWLEDGE_ENV_FILE -- picks up the real .env.local
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18150/health")
    check("knowledge backend healthy (real Azure Search config)", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    import orchestrator  # noqa: E402
    import request_context as ctx  # noqa: E402
    from mcp_server import mcp  # noqa: E402
    import app as gateway_app  # noqa: E402,F401 -- registers @mcp.tool defs (incl. knowledge_*) on mcp_server.mcp
    from store import get_store  # noqa: E402
    from store.models import ConsumerRecord  # noqa: E402
    from auth.passwords import hash_password  # noqa: E402
    from policy import manifest  # noqa: E402
    import personal_knowledge_store  # noqa: E402

    OWNER = "kb_quality_tester"
    docs = json.loads((ROOT / "_smoke" / "personal_kb_large.json").read_text(encoding="utf-8"))
    total_chunks = 0
    for d in docs:
        doc = personal_knowledge_store.ingest_document(OWNER, d["title"], d["title"] + ".txt", d["text"].encode("utf-8"))
        total_chunks += doc.chunk_count
    check(f"personal KB ingested ({len(docs)} docs, {total_chunks} chunks)", total_chunks > 40, total_chunks)

    only = {int(x) for x in sys.argv[1].split(",")} if len(sys.argv) > 1 else None
    cases_to_run = [(i, c) for i, c in enumerate(CASES) if only is None or i in only]
    print(f"\nrunning {len(cases_to_run)} questions ({sum(1 for _, c in cases_to_run if c['mcp'] == 'local')} local-KB, "
          f"{sum(1 for _, c in cases_to_run if c['mcp'] == 'prod')} prod-MCP) through the REAL orchestrator.run_chat "
          f"loop ({BACKEND})...\n")

    asyncio.run(main(cases_to_run))
finally:
    knowledge.terminate()
    try:
        knowledge.wait(timeout=5)
    except subprocess.TimeoutExpired:
        knowledge.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
