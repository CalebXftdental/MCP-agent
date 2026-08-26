"""End-to-end test of "Big-Spender Drop-Off" as a real, user-buildable "My
Workflow" graph -- the most structurally complex of the three new workflows in
this session (first real production use of the generic `join` node beyond its
own test coverage, see test_join_node.py), built and confidence-checked AFTER
test_unreachable_account_flag_graph.py proved the graph-building API recipe
end-to-end on the simplest of the three.

  trigger (country/state/city, start_date/end_date, limit, stale_days)
    -> tool_call: get_top_customers_by_spend (start_date/end_date/limit <- trigger)  ─┐
    -> tool_call: get_customer_order_recency (country/state/city <- trigger)         ─┤ both fed
                                                                                        directly by
                                                                                        the trigger,
                                                                                        run independently
    -> join: left <- top spenders (topCustomers), right <- order recency (customers),
             left_key=right_key="customerId" (the one field name BOTH tools' output
             rows genuinely share -- confirmed straight from
             mcp-minierp/sqlagent/analytics.py's get_top_customers_by_spend/
             get_customer_order_recency, no rename needed for the join key itself),
             fields=["lastOrderDate"] (the one field this workflow actually needs off
             the right side; a plain name, not "*", because both rows also carry their
             own unrelated "name"/"orderCount" that would otherwise collide),
             on_missing="drop" -- a top spender NOT found in this territory's recency
             page can't be evaluated for staleness at all, so it's excluded from
             `merged` (still reported in `unmatched`, never silently invisible)
    -> filter: input <- join's "merged"; lastOrderDate older_than_days stale_days
    -> tool_call: create_pdf_packet (tables <- filter's "matchedTable")
    -> tool_call: create_email_draft (never sent)

Real-data risk unique to this workflow (flagged up front, same "probe before
assuming" discipline test_ar_credit_hold_radar_graph.py's fix demonstrated):
get_top_customers_by_spend ranks customers COMPANY-WIDE, while
get_customer_order_recency is scoped to ONE territory -- there's a real chance a
territory query and the global top-spender list simply don't intersect on this
mirror's actual data, independent of anything being broken. This test probes
several territories up front (same candidate list the win-back/AR reference
tests use) and picks whichever gives the join something to actually match; if
none do, it still proves the join/filter MECHANISM ran correctly over live data
end-to-end (consistent unmatched/matched bookkeeping) rather than faking a
match that isn't there.
"""
from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "big-spender-drop-off-graph"
OUT = ROOT / "_smoke" / ".tmp" / "big-spender-drop-off-graph-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "big_spender_graph_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "big_spender_graph_password",
    "GOVERNANCE_SESSION_SECRET": "big-spender-drop-off-graph-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18581/mcp",
    "OFFICE_MCP_URL": "http://127.0.0.1:18582/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18583/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "30",
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


def pdf_text(payload: bytes) -> str:
    raw = payload.decode("latin-1", errors="replace")
    return "\n".join(m.group(1).replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
                      for m in re.finditer(r"\(((?:[^()\\]|\\.)*)\)\s*Tj", raw))


print("start mcp-minierp (real ERP mirror) + mcp-office + mcp-email")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
minierp = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18581", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18582", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18583", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18581/health")
    wait_health("http://127.0.0.1:18582/health")
    wait_health("http://127.0.0.1:18583/health")
    check("mcp-minierp (real ERP mirror) healthy", True)
    check("mcp-office healthy", True)
    check("mcp-email healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")
    from store import get_store  # noqa: E402
    from store.models import ConsumerRecord  # noqa: E402
    from auth.passwords import hash_password  # noqa: E402

    store = get_store()
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:big_spender_graph_builder", name="big_spender_graph_builder", key_hash="", status="active",
        role="user", type="user", categories=["analytics", "office", "email_draft"],
        login_password_hash=hash_password("builder_password"),
    ))

    def try_tool(client, tool, args, customer_id=""):
        return client.post("/dashboard/try-tool", json={"tool": tool, "args": args, "customer_id": customer_id})

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "big_spender_graph_builder", "password": "builder_password"})

        # ── Probe: company-wide top spenders (territory-independent) ──
        top_resp = try_tool(client, "get_top_customers_by_spend", {"limit": 100})
        check("get_top_customers_by_spend reachable on the live mirror", top_resp.status_code == 200, top_resp.text[:300])
        top_customers = ((top_resp.json() or {}).get("result") or {}).get("topCustomers") or []
        check("top-spenders probe returned real ranked customers", bool(top_customers), top_customers[:3])
        top_ids = {c.get("customerId") for c in top_customers if c.get("customerId")}

        # ── Probe several territories, picking whichever overlaps the top-spender set most ──
        best_territory, best_overlap, tried = None, -1, []
        for kwargs in (
            {"country": "US"}, {"country": "USA"}, {"state": "CA"}, {"country": "US", "state": "CA"},
            {"country": "CA"}, {"state": "TX"}, {"city": "a"},
        ):
            resp = try_tool(client, "get_customer_order_recency", {**kwargs, "page": 1, "page_size": 100})
            tried.append((kwargs, resp.status_code))
            if resp.status_code != 200:
                continue
            customers = ((resp.json() or {}).get("result") or {}).get("customers") or []
            if not customers:
                continue
            overlap = len({c.get("customerId") for c in customers if c.get("customerId")} & top_ids)
            if best_territory is None or overlap > best_overlap:
                best_territory, best_overlap = kwargs, overlap
        check("found at least one territory with real order-recency data via get_customer_order_recency",
              best_territory is not None, tried)
        print(f"  info best territory {best_territory} overlaps the top-100-spenders list on {best_overlap} customerId(s)")

        if best_territory is None:
            print("Could not reach a territory with data on the live ERP mirror from this environment.")
        else:
            graph = client.post("/workflow-graphs", json={
                "displayName": "Big-Spender Drop-Off",
                "description": "Cross-references company-wide top spenders with a territory's order-recency data "
                                "and flags any top spender who is overdue to reorder.",
                "nodes": [
                    {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": [
                        {"name": "country", "label": "Country", "optional": True},
                        {"name": "state", "label": "State", "optional": True},
                        {"name": "city", "label": "City", "optional": True},
                        {"name": "start_date", "label": "Spend period start", "optional": True},
                        {"name": "end_date", "label": "Spend period end", "optional": True},
                        {"name": "limit", "label": "Top-N customers by spend", "optional": True},
                        {"name": "stale_days", "label": "Drop-off threshold (days)"},
                    ]}},
                    {"nodeId": "n1", "kind": "tool_call", "tool": "get_top_customers_by_spend",
                     "title": "Rank customers by total spend",
                     "inputBindings": {
                         "start_date": {"source": "trigger", "path": "start_date"},
                         "end_date": {"source": "trigger", "path": "end_date"},
                         "limit": {"source": "trigger", "path": "limit"},
                     },
                     "config": {}},
                    {"nodeId": "n2", "kind": "tool_call", "tool": "get_customer_order_recency",
                     "title": "Look up order recency by territory",
                     "inputBindings": {
                         "country": {"source": "trigger", "path": "country"},
                         "state": {"source": "trigger", "path": "state"},
                         "city": {"source": "trigger", "path": "city"},
                     },
                     "config": {"page": 1, "page_size": 100}},
                    {"nodeId": "n3", "kind": "join", "title": "Cross-reference top spenders with recency",
                     "inputBindings": {
                         "left": {"source": "node", "node_id": "n1", "path": "topCustomers"},
                         "right": {"source": "node", "node_id": "n2", "path": "customers"},
                     },
                     "config": {
                         "left_key": "customerId", "right_key": "customerId",
                         "fields": ["lastOrderDate"], "on_missing": "drop",
                         "table_name": "Big Spenders x Order Recency",
                     }},
                    {"nodeId": "n4", "kind": "filter", "title": "Flag drop-off-overdue big spenders",
                     "inputBindings": {"input": {"source": "node", "node_id": "n3", "path": "merged"}},
                     "config": {
                         "table_name": "Big-Spender Drop-Off",
                         "conditions": {"all": [
                             {"field": "lastOrderDate", "op": "older_than_days", "value": {"source": "trigger", "path": "stale_days"}},
                         ]},
                     }},
                    {"nodeId": "n5", "kind": "tool_call", "tool": "create_pdf_packet",
                     "title": "Build big-spender drop-off PDF",
                     "inputBindings": {"tables": {"source": "node", "node_id": "n4", "path": "matchedTable"}},
                     "config": {
                         "title": "Big-Spender Drop-Off",
                         "sections": [{"heading": "Big-Spender Drop-Off", "bullets": [
                             "Cross-references company-wide top spenders with this territory's order-recency data.",
                             "Flags top spenders who are overdue to reorder -- see the attached table.",
                         ]}],
                         "classification": ["INTERNAL", "SENSITIVE"],
                     }},
                    {"nodeId": "n6", "kind": "tool_call", "tool": "create_email_draft",
                     "title": "Draft big-spender drop-off digest email",
                     "config": {
                         "to": ["account-management@frontierdental.com"],
                         "subject": "Big-Spender Drop-Off: top accounts overdue to reorder",
                         "body_markdown": "Hi team,\n\nThe attached report flags top-spending accounts in this "
                                          "territory that are overdue to reorder. Please review and follow up.\n\n"
                                          "Regards,\nGoverned AI Office Assistant",
                         "classification": ["INTERNAL"],
                     }},
                ],
                "edges": [
                    {"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n1"},
                    {"edgeId": "e2", "sourceNodeId": "trigger", "targetNodeId": "n2"},
                    {"edgeId": "e3", "sourceNodeId": "n1", "targetNodeId": "n3"},
                    {"edgeId": "e4", "sourceNodeId": "n2", "targetNodeId": "n3"},
                    {"edgeId": "e5", "sourceNodeId": "n3", "targetNodeId": "n4"},
                    {"edgeId": "e6", "sourceNodeId": "n4", "targetNodeId": "n5"},
                    {"edgeId": "e7", "sourceNodeId": "n5", "targetNodeId": "n6"},
                ],
            })
            check("big-spender drop-off graph created", graph.status_code == 201, graph.text)
            gid = graph.json()["graphId"]
            pub = client.post(f"/workflow-graphs/{gid}/publish", json={})
            check("big-spender drop-off graph published", pub.status_code == 200, pub.text)

            # stale_days=1 -- deliberately tiny (see test_winback_radar_graph.py's
            # identical rationale): the real data distribution isn't known ahead of
            # time, so "ordered more than a day ago" is the honest way to prove the
            # mechanism without assuming what the joined accounts' order history
            # looks like.
            run = client.post(f"/workflows/{gid}/run", json={**best_territory, "limit": 100, "stale_days": 1})
            run_body = run.json()
            check("big-spender drop-off run reaches the join/filter steps", run.status_code in (201, 409), run_body)
            steps_by_id = {s.get("stepId"): s for s in run_body.get("steps", [])}

            n1_out = steps_by_id.get("n1", {}).get("outputs", {})
            n2_out = steps_by_id.get("n2", {}).get("outputs", {})
            check("n1 (get_top_customers_by_spend) returned real ranked customers", bool(n1_out.get("topCustomers")), n1_out)
            check("n2 (get_customer_order_recency) returned real customers", bool(n2_out.get("customers")), n2_out)

            n3_out = steps_by_id.get("n3", {}).get("outputs", {})
            check("n3 (join) ran to completion over real data (matchedCount/unmatchedCount/totalCount are real ints)",
                  isinstance(n3_out.get("matchedCount"), int) and isinstance(n3_out.get("unmatchedCount"), int)
                  and n3_out.get("totalCount") == len(n1_out.get("topCustomers") or []), n3_out)
            merged = n3_out.get("merged") or []
            check("every merged row carries the joined-in lastOrderDate key (present, though possibly "
                  "None for an account that never ordered)", all("lastOrderDate" in row for row in merged), merged[:5])
            check("every merged row genuinely was a real top-spender/recency match (regression check)",
                  all(row.get("customerId") in top_ids for row in merged), merged[:5])

            n4_out = steps_by_id.get("n4", {}).get("outputs", {})
            matched = n4_out.get("matchedCount") or 0
            print(f"  info join matched={n3_out.get('matchedCount')} big spenders found in this territory's recency "
                  f"data; of those, filter flagged {matched} as drop-off-overdue (stale_days=1)")
            check("filter step ran to completion over the joined data (matchedCount is a real int, not an error)",
                  isinstance(matched, int) and matched >= 0, n4_out)
            flagged_rows = n4_out.get("matched") or []
            check("every flagged row genuinely carries a real lastOrderDate (regression check)",
                  all(row.get("lastOrderDate") for row in flagged_rows), flagged_rows[:5])

            if run_body.get("status") == "approval_required":
                approval_id = (run_body.get("approval") or {}).get("approvalId")
                check("broad-export approval was actually created", bool(approval_id), run_body)
                client.post("/dashboard/logout")
                client.post("/dashboard/login", json={"username": "big_spender_graph_admin", "password": "big_spender_graph_password"})
                approve = client.post(f"/approvals/{approval_id}/approve", json={"note": "reviewed big-spender drop-off report"})
                check("admin approves the broad export", approve.status_code == 200, approve.text)
                resumed = client.post(f"/workflow-runs/{run_body['runId']}/resume", json={"approval_id": approval_id})
                run_body = resumed.json()
                check("resume completes the run", resumed.status_code == 200 and run_body.get("status") == "completed", run_body)
                client.post("/dashboard/logout")
                client.post("/dashboard/login", json={"username": "big_spender_graph_builder", "password": "builder_password"})
            else:
                check("big-spender drop-off run completes without needing approval", run_body.get("status") == "completed", run_body)

            n5_step = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n5"), {})
            aid = n5_step.get("outputs", {}).get("artifactId")
            check("big-spender drop-off PDF artifact created", bool(aid), n5_step)

            if aid:
                dl = client.get(f"/artifacts/{aid}/download")
                check("PDF artifact downloads", dl.status_code == 200, dl.status_code)
                (OUT / "big-spender-drop-off.pdf").write_bytes(dl.content)
                text = pdf_text(dl.content)
                if flagged_rows:
                    real_id = str(flagged_rows[0].get("customerId") or "")
                    check("PDF contains a real flagged customer id from the live mirror", real_id in text, text[:600])
                    check("PDF has more than just a title (regression check)", len(text.splitlines()) > 5, text)
                else:
                    check("empty-result PDF is still non-trivial (has a title/section, not blank)", len(text.splitlines()) > 0, text)

            n6_step = steps_by_id.get("n6", {}) if steps_by_id.get("n6", {}).get("status") == "completed" else next(
                (s for s in run_body.get("steps", []) if s.get("stepId") == "n6"), {})
            check("digest email drafted, never sent (draftId present, no sendId)",
                  n6_step.get("status") == "completed" and n6_step.get("outputs", {}).get("draftId") and not n6_step.get("outputs", {}).get("sendId"),
                  n6_step)

finally:
    minierp.terminate()
    office.terminate()
    email.terminate()
    for proc in (minierp, office, email):
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

print(f"\n{PASS} passed, {FAIL} failed")
print(f"artifacts written to: {OUT}")
sys.exit(1 if FAIL else 0)
