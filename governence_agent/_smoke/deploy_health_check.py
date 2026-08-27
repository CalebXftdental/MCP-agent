"""Deployment health check -- runs against a LIVE, already-deployed gateway
(not local dev processes) and reports PASS/FAIL for every layer of the
governed pipeline in one run. See ../deploy_health_check_test_fields.md for
the plan this implements and the reasoning behind the tier/scope choices
below.

Auth model (two SEPARATE surfaces -- see governance_core/edge.py):
  - MCP plane (/mcp): Bearer API key. This script authenticates here with
    the admin key already used by _smoke/test_winback_radar_deployed.py,
    read from mcp-knowledge/.env.local's `local_admin_api_key` line unless
    --api-key overrides it. Covers layers 1-9 of the plan (auth, transport,
    federation, policy, all 6 backends, artifacts, workflow graphs, KB).
  - Dashboard plane (/dashboard*, /admin/*, /chat): session cookie, NOT the
    API key. These checks (audit-trail visibility, RBAC, and -- notably --
    the live chat/LLM tool-calling path, which nothing else here exercises)
    only run if you pass --dashboard-user/--dashboard-password. Omitted ->
    they're SKIPped, not failed, since the admin API key alone can't reach
    them.

Tiers:
  - Tier 1 (default, always runs): read-only / side-effect-free. Failing
    any Tier-1 check fails the run (exit 1).
  - Tier 2 (--tier2): creates real records in the deployment (a PDF
    artifact, an email draft, a calendar draft, a personal-KB document, a
    workflow-graph draft). Everything it creates is prefixed
    "[healthcheck]" and self-cleans where a delete tool exists (personal KB
    docs); PDF artifacts and workflow-graph drafts have no delete tool
    today, so they're left labeled rather than blocked on building one.
    A Tier-2 failure is reported but does not flip the exit code.

Usage:
    python _smoke/deploy_health_check.py --url https://<your-app>.azurewebsites.net
    python _smoke/deploy_health_check.py --url https://<your-app>.azurewebsites.net --tier2
    python _smoke/deploy_health_check.py --url https://<your-app>.azurewebsites.net \\
        --tier2 --dashboard-user admin --dashboard-password ***
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).parent.parent.resolve()

BACKEND_TOOL_PREFIXES = [
    "minierp_orders_", "minierp_accounts_", "minierp_finance_",
    "minierp_shipments_", "minierp_analytics_", "office_", "email_",
    "calendar_", "knowledge_", "code_",
]

PASS = FAIL1 = FAIL2 = SKIPPED = 0


def check(name, cond, extra=None, tier=1):
    global PASS, FAIL1, FAIL2
    if cond:
        PASS += 1
        print(f"  ok   {name}")
        return
    detail = f"  -- {str(extra)[:400]}" if extra is not None else ""
    if tier == 1:
        FAIL1 += 1
        print(f"  FAIL {name}{detail}")
    else:
        FAIL2 += 1
        print(f"  FAIL(t2) {name}{detail}")


def skip(name, reason):
    global SKIPPED
    SKIPPED += 1
    print(f"  skip {name} -- {reason}")


def pdf_text(payload: bytes) -> str:
    raw = payload.decode("latin-1", errors="replace")
    return "\n".join(m.group(1).replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
                      for m in re.finditer(r"\(((?:[^()\\]|\\.)*)\)\s*Tj", raw))


def read_default_api_key() -> str | None:
    f = ROOT / "mcp-knowledge" / ".env.local"
    if not f.exists():
        return None
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("local_admin_api_key"):
            return line.split("=", 1)[1].strip()
    return None


# ── [1] edge / auth (no valid credentials needed -- these ARE the negative case) ──
def run_edge_auth_checks(base: str) -> None:
    print("\n[1] edge / auth")
    try:
        r = httpx.post(f"{base}/mcp", json={}, timeout=15)
        check("reject /mcp with no Authorization header", r.status_code in (401, 400), r.status_code)
    except Exception as exc:  # noqa: BLE001
        check("reject /mcp with no Authorization header", False, exc)
    try:
        r = httpx.post(f"{base}/mcp", json={}, headers={"Authorization": "Bearer not-a-real-key"}, timeout=15)
        check("reject /mcp with a bad API key", r.status_code == 401, r.status_code)
    except Exception as exc:  # noqa: BLE001
        check("reject /mcp with a bad API key", False, exc)


# ── [2] static / frontend serving (no auth needed) ──────────────────────────
def run_static_checks(base: str) -> None:
    print("\n[2] static / frontend serving")
    try:
        r = httpx.get(f"{base}/", timeout=15, follow_redirects=True)
        check("GET / serves (200)", r.status_code == 200, r.status_code)
    except Exception as exc:  # noqa: BLE001
        check("GET / serves (200)", False, exc)
    try:
        # /dashboard is the SPA shell, itself auth-gated -- 401 without a
        # session is the HEALTHY answer here, not a failure.
        r = httpx.get(f"{base}/dashboard", timeout=15)
        check("GET /dashboard is reachable and auth-gated (401, not a crash)", r.status_code == 401, r.status_code)
    except Exception as exc:  # noqa: BLE001
        check("GET /dashboard is reachable and auth-gated (401, not a crash)", False, exc)


# ── [3] MCP plane: transport, federation, PDP, scope, all 6 backends ────────
async def run_mcp_checks(mcp_url: str, api_key: str, tier2: bool, base: str):
    print("\n[3] MCP transport, federation, policy plane, backends")
    headers = {"Authorization": f"Bearer {api_key}"}
    marker = f"healthcheck-{uuid.uuid4().hex[:8]}"
    session_id = f"deploy-health-check-{uuid.uuid4().hex[:8]}"
    try:
        async with streamablehttp_client(mcp_url, headers=headers, timeout=60) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                await session.initialize()
                check("MCP session initializes against the deployed gateway", True)

                tools = (await session.list_tools()).tools
                names = {t.name for t in tools}
                check(f"tools/list returns tools ({len(names)} total)", len(names) > 0)
                for prefix in BACKEND_TOOL_PREFIXES:
                    matched = sorted(n for n in names if n.startswith(prefix))
                    check(f"backend federated: at least one {prefix}* tool", len(matched) > 0, sorted(names)[:10])

                async def call(tool, args):
                    res = await session.call_tool(tool, arguments={**args, "session_id": session_id})
                    text = ""
                    for part in res.content or []:
                        if getattr(part, "text", None) is not None:
                            text = part.text
                            break
                    return getattr(res, "isError", False), text

                # -- discover a real customer, same probing approach as
                # test_winback_radar_deployed.py, so downstream checks use
                # real data instead of a hardcoded id that could go stale --
                customers = []
                for kwargs in ({"country": "US"}, {"country": "USA"}, {"state": "CA"},
                                {"country": "CA"}, {"state": "TX"}, {"state": "ON"}, {"city": "a"}):
                    is_error, text = await call("minierp_accounts_get_customers_by_region",
                                                 {**kwargs, "page": 1, "page_size": 5})
                    if is_error:
                        continue
                    try:
                        result = json.loads(text)
                    except (ValueError, TypeError):
                        continue
                    found = result.get("customers") or []
                    if found:
                        customers = found
                        break
                check("discovered at least one real customer for downstream checks", len(customers) > 0)
                cust_id = customers[0].get("customerId") if customers else ""

                # accounts
                is_error, text = await call("minierp_accounts_find_customer", {"query": cust_id or "a", "by": "auto"})
                check("minierp_accounts_find_customer responds", not is_error, text[:300])
                if cust_id:
                    is_error, text = await call("minierp_accounts_get_customer_overview", {"customer_id": cust_id})
                    check("minierp_accounts_get_customer_overview returns real data", not is_error and cust_id in text, text[:300])
                else:
                    skip("minierp_accounts_get_customer_overview", "no discovered customer_id")

                # orders + session scope memory (PDP scope_store round-trip)
                if cust_id:
                    is_error, text = await call("minierp_orders_get_customer_orders",
                                                 {"customer_id": cust_id, "page_size": 5})
                    check("minierp_orders_get_customer_orders responds", not is_error, text[:300])
                    is_error2, text2 = await call("minierp_orders_get_customer_order_total", {})
                    check("session scope remembers customer_id across calls (no customer_id on 2nd call)",
                          not is_error2, text2[:300])
                else:
                    skip("orders + session-scope-memory checks", "no discovered customer_id")

                # finance (cross-account, no discovered-data dependency)
                is_error, text = await call("minierp_finance_get_ap_invoices_due_soon",
                                             {"days_ahead": 14, "page_size": 25})
                check("minierp_finance_get_ap_invoices_due_soon responds", not is_error, text[:300])

                # shipments
                if cust_id:
                    is_error, text = await call("minierp_shipments_get_customer_shipment_status",
                                                 {"customer_id": cust_id})
                    check("minierp_shipments_get_customer_shipment_status responds", not is_error, text[:300])
                else:
                    skip("minierp_shipments_get_customer_shipment_status", "no discovered customer_id")

                # analytics (cross-customer, entitlement-gated)
                is_error, text = await call("minierp_analytics_get_top_customers_by_spend", {"limit": 5})
                check("minierp_analytics_get_top_customers_by_spend responds (analytics entitlement present)",
                      not is_error, text[:300])

                # knowledge (company tier -- read-only, no ingest path anymore)
                is_error, text = await call("knowledge_search_knowledge", {"query": "policy", "limit": 3})
                check("knowledge_search_knowledge responds", not is_error, text[:300])

                # code (plan-only -- never applies changes)
                is_error, text = await call("code_opencode_plan_change", {
                    "request": "Deployment health check -- no-op plan request, do not implement.",
                    "risk_level": "low",
                })
                check("code_opencode_plan_change responds (plan-only, no repo changes)", not is_error, text[:300])

                # workflow graph plane -- read-only half (list only; create/edit is Tier 2)
                is_error, text = await call("list_my_workflows", {})
                check("list_my_workflows responds", not is_error, text[:300])

                if tier2:
                    print("\n[3b] tier-2 write-path checks (--tier2)")
                    try:
                        await run_tier2_checks(call, marker, base, headers)
                    except Exception as exc:  # noqa: BLE001
                        check("tier-2 checks completed without crashing", False, exc, tier=2)
                else:
                    skip("tier-2 write-path checks (PDF/email/calendar/personal-KB/workflow-graph)",
                         "pass --tier2 to include")
    except Exception as exc:  # noqa: BLE001
        check("MCP-plane checks completed without a transport-level crash", False, exc)
    return marker


async def run_tier2_checks(call, marker: str, base: str, headers: dict) -> None:
    title = f"[healthcheck] {marker}"

    # -- office artifact round-trip --
    is_error, text = await call("office_create_pdf_packet", {
        "title": title,
        "sections": [{"heading": "Deployment Health Check", "bullets": [f"marker: {marker}"]}],
        "tables": [],
        "classification": ["INTERNAL"],
    })
    check("office_create_pdf_packet creates an artifact", not is_error, text[:300], tier=2)
    result = json.loads(text) if not is_error else {}
    download_url = result.get("downloadUrl")
    check("PDF artifact has a downloadUrl", bool(download_url), result, tier=2)
    if download_url:
        full_url = download_url if download_url.startswith("http") else base + download_url
        async with httpx.AsyncClient(headers=headers, timeout=30) as http:
            dl = await http.get(full_url)
        check("PDF artifact downloads", dl.status_code == 200, dl.status_code, tier=2)
        if dl.status_code == 200:
            text_out = pdf_text(dl.content)
            check("downloaded PDF contains our marker (content sanity, not just a title)",
                  marker in text_out, text_out[:300], tier=2)

    # -- email draft (never sent) --
    is_error, text = await call("email_create_email_draft", {
        "to": ["healthcheck@example.invalid"],
        "subject": title,
        "body_markdown": f"Deployment health check marker: {marker}",
        "classification": ["INTERNAL"],
    })
    check("email_create_email_draft creates a draft", not is_error, text[:300], tier=2)
    draft = json.loads(text) if not is_error else {}
    check("email draft created, NOT sent (draftId present, no sendId)",
          bool(draft.get("draftId")) and not draft.get("sendId"), draft, tier=2)

    # -- calendar draft (never creates an external event) --
    start = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=7, hours=1)).isoformat()
    is_error, text = await call("calendar_draft_calendar_invite", {
        "title": title, "start": start, "end": end, "attendees": ["healthcheck@example.invalid"],
    })
    check("calendar_draft_calendar_invite creates a draft", not is_error, text[:300], tier=2)

    # -- personal knowledge tier: ingest -> search -> delete (self-cleaning) --
    content = f"Deployment health check document. marker={marker}".encode("utf-8")
    is_error, text = await call("knowledge_ingest_my_document", {
        "title": title, "filename": "healthcheck.txt",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "classification": "INTERNAL",
    })
    check("knowledge_ingest_my_document ingests a private doc", not is_error, text[:300], tier=2)
    ingested = json.loads(text) if not is_error else {}
    doc_id = ingested.get("documentId") or ingested.get("document_id")

    is_error, text = await call("knowledge_search_my_documents", {"query": marker, "limit": 5})
    check("knowledge_search_my_documents finds the ingested doc", not is_error and marker in text, text[:300], tier=2)

    if doc_id:
        is_error, text = await call("knowledge_delete_my_document", {"document_id": doc_id})
        check("knowledge_delete_my_document cleans up (self-cleaning check)", not is_error, text[:300], tier=2)
    else:
        skip("knowledge_delete_my_document (cleanup)", "no documentId returned from ingest -- nothing to delete")

    # -- workflow graph draft lifecycle (no delete tool today -- left labeled) --
    is_error, text = await call("propose_graph", {
        "display_name": title,
        "description": "Deployment health check draft -- safe to delete from the My Workflow page.",
        "nodes": [
            {"nodeId": "trigger1", "kind": "trigger", "title": "Start",
             "config": {"inputs": [{"name": "query", "label": "Query"}]}},
            {"nodeId": "find1", "kind": "tool_call", "title": "Find Customer",
             "tool": "minierp_accounts_find_customer", "config": {},
             "inputBindings": {"query": {"source": "trigger", "path": "query"}}},
        ],
        "edges": [{"edgeId": "e1", "sourceNodeId": "trigger1", "targetNodeId": "find1"}],
    })
    check("propose_graph saves a valid draft workflow", not is_error, text[:400], tier=2)
    graph = json.loads(text) if not is_error else {}
    graph_id = graph.get("graphId")

    is_error, text = await call("list_my_workflows", {})
    check("list_my_workflows includes the new draft", not is_error and bool(graph_id) and graph_id in text,
          text[:300], tier=2)

    if graph_id:
        is_error, text = await call("get_my_workflow", {"graph_id": graph_id})
        check("get_my_workflow round-trips the draft's node", not is_error and "find1" in text, text[:400], tier=2)

    # invalid graph (no trigger node) must be REJECTED, not silently accepted
    is_error, text = await call("propose_graph", {
        "display_name": f"[healthcheck-invalid] {marker}",
        "nodes": [{"nodeId": "a", "kind": "tool_call", "title": "A",
                   "tool": "minierp_accounts_find_customer", "config": {}}],
        "edges": [],
    })
    invalid_result = json.loads(text) if text else {}
    check("propose_graph rejects a graph with no trigger node (validation is live, not bypassable)",
          invalid_result.get("status") == "error", text[:400], tier=2)


# ── [4] dashboard plane: login, RBAC, audit trail, live chat/LLM (optional) ──
def run_dashboard_checks(base: str, user: str, password: str, mcp_marker_tool: str) -> None:
    print("\n[4] dashboard session: login, RBAC, audit trail, live chat/LLM")
    c = httpx.Client(base_url=base, timeout=20)
    r = c.post("/dashboard/login", json={"username": user, "password": password})
    check("dashboard login succeeds", r.status_code == 200 and "gov_session" in c.cookies, r.status_code)
    if r.status_code != 200:
        skip("RBAC / audit-trail / live-chat checks", "login failed, see above")
        return

    me = c.get("/dashboard/me")
    check("/dashboard/me reachable", me.status_code == 200, me.status_code)

    calls = c.get("/admin/calls")
    check("/admin/calls reachable with this session", calls.status_code == 200, calls.status_code)
    if calls.status_code == 200:
        check("this run's own MCP-plane calls appear in the audit trail",
              mcp_marker_tool in calls.text, "expected tool name in audit body")

    rl = c.get("/admin/rate-limits")
    check("/admin/rate-limits reachable", rl.status_code == 200, rl.status_code)

    # -- live chat: the one path nothing else here exercises. Everything
    # above proves the MCP plane works; this proves the LLM broker + tool-
    # calling loop the actual chat UI depends on is *also* healthy. --
    chat_marker = f"deploy-health-check-{uuid.uuid4().hex[:8]}"
    try:
        chat_r = c.post("/chat", json={
            "message": "How many AP invoices are due in the next 14 days? Use the AP due-soon tool.",
            "conversation_id": chat_marker,
        }, timeout=90)
        check("POST /chat responds (LLM broker + tool-loop reachable)", chat_r.status_code == 200, chat_r.status_code)
        if chat_r.status_code == 200:
            body = chat_r.json()
            check("chat produced a non-empty reply", bool((body.get("reply") or "").strip()), body)
            check("chat actually called a tool (live model, not a canned/error reply)",
                  bool(body.get("tool_calls")), body.get("tool_calls"))
    except Exception as exc:  # noqa: BLE001
        check("POST /chat responds (LLM broker + tool-loop reachable)", False, exc)

    c.post("/dashboard/logout")
    after = c.get("/admin/calls")
    check("logout invalidates the session (401 after)", after.status_code == 401, after.status_code)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="Gateway base URL, e.g. https://<app>.azurewebsites.net")
    p.add_argument("--api-key", default=None,
                   help="MCP Bearer API key. Defaults to local_admin_api_key from mcp-knowledge/.env.local")
    p.add_argument("--tier2", action="store_true",
                   help="Also run write-path checks (creates real artifacts/drafts, prefixed [healthcheck]).")
    p.add_argument("--dashboard-user", default=None, help="Dashboard username, to also run session-gated checks.")
    p.add_argument("--dashboard-password", default=None, help="Dashboard password.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    base = args.url.rstrip("/")
    mcp_url = base + "/mcp"
    api_key = args.api_key or read_default_api_key()
    if not api_key:
        print("No API key available -- pass --api-key or ensure mcp-knowledge/.env.local has "
              "a local_admin_api_key= line.")
        sys.exit(2)

    dashboard_enabled = bool(args.dashboard_user and args.dashboard_password)
    print("== Deployment Health Check ==")
    print(f"target:    {base}")
    print(f"tier2:     {'on' if args.tier2 else 'off (pass --tier2 to include write-path checks)'}")
    print(f"dashboard: {'on' if dashboard_enabled else 'off (pass --dashboard-user/--dashboard-password to include)'}")

    run_edge_auth_checks(base)
    run_static_checks(base)
    asyncio.run(run_mcp_checks(mcp_url, api_key, args.tier2, base))

    if dashboard_enabled:
        run_dashboard_checks(base, args.dashboard_user, args.dashboard_password, "get_ap_invoices_due_soon")
    else:
        skip("dashboard session checks (audit trail / RBAC / live chat)",
             "pass --dashboard-user/--dashboard-password to include -- the API key alone can't reach /dashboard or /admin")

    print(f"\n{'=' * 60}")
    print(f"{PASS} passed, {FAIL1} failed, {FAIL2} tier-2 failed, {SKIPPED} skipped")
    print(f"{'=' * 60}")
    sys.exit(1 if FAIL1 else 0)


if __name__ == "__main__":
    main()
