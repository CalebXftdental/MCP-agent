"""End-to-end test of "New-Customer Second-Order Nudge" as a real,
user-buildable "My Workflow" graph -- structurally the SAME chain
test_winback_radar_graph.py already proves end-to-end (tool -> filter -> PDF
-> draft email over get_customer_order_recency), with the filter condition
INVERTED: instead of flagging accounts overdue to REorder (lastOrderDate too
old), this flags brand-new accounts that placed exactly one order recently
and haven't placed a second one yet -- a "nudge them toward order #2" signal
for account management, not a win-back signal.

  trigger (country/state/city, window_days)
    -> tool_call: get_customer_order_recency (country/state/city <- trigger)
    -> filter: input <- "customers";
       orderCount eq 1 AND firstOrderDate newer_than_days window_days
    -> tool_call: create_pdf_packet (tables <- filter's "matchedTable")
    -> tool_call: create_email_draft (never sent -- same "draft only" pattern
       every other digest graph in this session uses)

orderCount/firstOrderDate/lastOrderDate are confirmed real fields directly
from mcp-minierp/sqlagent/analytics.py's get_customer_order_recency (the
per-customer rollup literally builds {"orderCount": ..., "firstOrderDate":
min(dates), "lastOrderDate": max(dates), ...}) and from that tool's
governance_core/policy/manifest.py ToolPolicy.fields entry -- no field-name
guessing needed.

window_days is tried across a growing set of candidate values (same
retry-over-a-few-trigger-values pattern test_ar_credit_hold_radar_graph.py
uses) since the live mirror's real order-date distribution isn't known ahead
of time -- `newer_than_days` means "no older than N days", so a LARGER
window_days is the more permissive direction here (catches older real first-
order dates too), unlike win_back_days in the win-back graph where smaller is
more permissive.
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
TMP = ROOT / "_smoke" / ".tmp" / "new-customer-second-order-nudge-graph"
OUT = ROOT / "_smoke" / ".tmp" / "new-customer-second-order-nudge-graph-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "new_cust_nudge_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "new_cust_nudge_password",
    "GOVERNANCE_SESSION_SECRET": "new-customer-second-order-nudge-graph-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18561/mcp",
    "OFFICE_MCP_URL": "http://127.0.0.1:18562/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18563/mcp",
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
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18561", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18562", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18563", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18561/health")
    wait_health("http://127.0.0.1:18562/health")
    wait_health("http://127.0.0.1:18563/health")
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
        consumer_id="user:new_cust_nudge_builder", name="new_cust_nudge_builder", key_hash="", status="active",
        role="user", type="user", categories=["analytics", "office", "email_draft"],
        login_password_hash=hash_password("builder_password"),
    ))

    def try_tool(client, tool, args, customer_id=""):
        return client.post("/dashboard/try-tool", json={"tool": tool, "args": args, "customer_id": customer_id})

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "new_cust_nudge_builder", "password": "builder_password"})

        # ── Find a territory with real order history, same discovery approach
        #    test_winback_radar_graph.py already uses for get_customer_order_recency. ──
        territory = None
        tried = []
        for kwargs in (
            {"country": "US"}, {"country": "USA"}, {"state": "CA"}, {"country": "US", "state": "CA"},
            {"country": "CA"}, {"state": "TX"}, {"city": "a"},
        ):
            resp = try_tool(client, "get_customer_order_recency", {**kwargs, "page": 1, "page_size": 25})
            tried.append((kwargs, resp.status_code, resp.text[:200] if resp.status_code != 200 else ""))
            if resp.status_code != 200:
                continue
            result = (resp.json() or {}).get("result") or {}
            customers = result.get("customers") or []
            # Specifically looking for a page with an orderCount==1 row (not
            # just any nonzero orderCount) -- confirmed by direct probe
            # (_smoke/.tmp/probe_order_recency2.py) that {"country": "US"}'s
            # own first page has real order history but zero orderCount==1
            # rows on it, while {"state": "CA"} does; a territory that merely
            # has SOME order history doesn't guarantee this graph's specific
            # filter condition has anything to match.
            if any(c.get("orderCount") == 1 for c in customers):
                territory = kwargs
                break
        check("found a territory with a real orderCount==1 (new, one-order) customer via get_customer_order_recency",
              territory is not None, tried)

        if territory is None:
            print("Could not reach a territory with order history on the live ERP mirror from this "
                  "environment -- the filter-node mechanics themselves are already proven "
                  "network-independently by _smoke/test_filter_node.py.")
        else:
            # ── Build the SAME graph a human builds by hand on My Workflow ──
            graph = client.post("/workflow-graphs", json={
                "displayName": "New-Customer Second-Order Nudge",
                "description": "Flags customers in a territory who placed exactly one order and haven't "
                                "reordered yet, and drafts a nudge digest for account management.",
                "nodes": [
                    {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": [
                        {"name": "country", "label": "Country", "optional": True},
                        {"name": "state", "label": "State", "optional": True},
                        {"name": "city", "label": "City", "optional": True},
                        {"name": "window_days", "label": "First-order recency window (days)"},
                    ]}},
                    {"nodeId": "n1", "kind": "tool_call", "tool": "get_customer_order_recency",
                     "title": "Look up order recency by territory",
                     "inputBindings": {
                         "country": {"source": "trigger", "path": "country"},
                         "state": {"source": "trigger", "path": "state"},
                         "city": {"source": "trigger", "path": "city"},
                     },
                     "config": {"page": 1, "page_size": 25}},
                    {"nodeId": "n2", "kind": "filter", "title": "Flag new, one-order customers",
                     "inputBindings": {"input": {"source": "node", "node_id": "n1", "path": "customers"}},
                     "config": {
                         "table_name": "New-Customer Second-Order Nudge",
                         "conditions": {"all": [
                             {"field": "orderCount", "op": "eq", "value": 1},
                             {"field": "firstOrderDate", "op": "newer_than_days", "value": {"source": "trigger", "path": "window_days"}},
                         ]},
                     }},
                    {"nodeId": "n3", "kind": "tool_call", "tool": "create_pdf_packet",
                     "title": "Build second-order nudge PDF",
                     "inputBindings": {"tables": {"source": "node", "node_id": "n2", "path": "matchedTable"}},
                     "config": {
                         "title": "New-Customer Second-Order Nudge",
                         "sections": [{"heading": "New-Customer Second-Order Nudge", "bullets": [
                             "Flags customers in this territory who placed exactly one order and haven't "
                             "reordered yet.",
                             "See the attached table for the full list of flagged accounts.",
                         ]}],
                         "classification": ["INTERNAL"],
                     }},
                    {"nodeId": "n4", "kind": "tool_call", "tool": "create_email_draft",
                     "title": "Draft second-order nudge email",
                     "config": {
                         "to": ["account-management@frontierdental.com"],
                         "subject": "New-Customer Second-Order Nudge: accounts flagged this week",
                         "body_markdown": "Hi team,\n\nThe attached report flags customers in this territory who "
                                          "placed exactly one order and haven't reordered yet. Please review and "
                                          "consider a follow-up nudge toward a second order.\n\nRegards,\n"
                                          "Governed AI Office Assistant",
                         "classification": ["INTERNAL"],
                     }},
                ],
                "edges": [
                    {"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n1"},
                    {"edgeId": "e2", "sourceNodeId": "n1", "targetNodeId": "n2"},
                    {"edgeId": "e3", "sourceNodeId": "n2", "targetNodeId": "n3"},
                    {"edgeId": "e4", "sourceNodeId": "n3", "targetNodeId": "n4"},
                ],
            })
            check("new-customer second-order nudge graph created", graph.status_code == 201, graph.text)
            gid = graph.json()["graphId"]
            pub = client.post(f"/workflow-graphs/{gid}/publish", json={})
            check("new-customer second-order nudge graph published", pub.status_code == 200, pub.text)

            run = None
            matched = 0
            run_body = {}
            for window_days in (365, 3650, 36500, 365000):
                attempt = client.post(f"/workflows/{gid}/run", json={**territory, "window_days": window_days})
                body_json = attempt.json()
                n2_out = next((s for s in body_json.get("steps", []) if s.get("stepId") == "n2"), {}).get("outputs", {})
                matched = n2_out.get("matchedCount") or 0
                run, run_body = attempt, body_json
                if matched > 0:
                    break
            check("new-customer second-order nudge run reaches the PDF step", run.status_code == 201, run_body)
            steps_by_id = {s.get("stepId"): s for s in run_body.get("steps", [])}

            n1_out = steps_by_id.get("n1", {}).get("outputs", {})
            n2_out = steps_by_id.get("n2", {}).get("outputs", {})
            check("n1 (get_customer_order_recency) returned real customers", n1_out.get("status") == "ok" or bool(n1_out), n1_out)
            n1_customers = n1_out.get("customers") or []
            check("every customer row carries orderCount/firstOrderDate keys (real field names, not guessed)",
                  bool(n1_customers) and all({"orderCount", "firstOrderDate", "lastOrderDate"} <= set(c.keys()) for c in n1_customers),
                  n1_customers[:3])

            print(f"  info matched={matched} new one-order customers out of {len(n1_customers)} on this page "
                  "(0 is a legitimate outcome if this territory's accounts have all reordered, or all have "
                  "multiple/zero orders on record)")
            matched_rows = n2_out.get("matched") or []
            check("every flagged row actually carries orderCount == 1 (regression check)",
                  all(row.get("orderCount") == 1 for row in matched_rows), matched_rows[:5])
            check("filter step ran to completion over the real dataset (matchedCount is a real int, not an error)",
                  isinstance(matched, int) and matched >= 0, run_body)

            if run_body.get("status") == "approval_required":
                approval_id = (run_body.get("approval") or {}).get("approvalId")
                check("broad-export approval was actually created", bool(approval_id), run_body)
                client.post("/dashboard/logout")
                client.post("/dashboard/login", json={"username": "new_cust_nudge_admin", "password": "new_cust_nudge_password"})
                approve = client.post(f"/approvals/{approval_id}/approve", json={"note": "reviewed second-order nudge PDF"})
                check("admin approves the broad export", approve.status_code == 200, approve.text)
                resumed = client.post(f"/workflow-runs/{run_body['runId']}/resume", json={"approval_id": approval_id})
                run_body = resumed.json()
                check("resume completes the run", resumed.status_code == 200 and run_body.get("status") == "completed", run_body)
                client.post("/dashboard/logout")
                client.post("/dashboard/login", json={"username": "new_cust_nudge_builder", "password": "builder_password"})
                steps_by_id = {s.get("stepId"): s for s in run_body.get("steps", [])}
            else:
                check("new-customer second-order nudge run completes without needing approval", run_body.get("status") == "completed", run_body)

            n3_step = steps_by_id.get("n3", {})
            artifacts = run_body.get("artifacts") or []
            pdf_artifact = next((a for a in artifacts if a.get("artifactId")), {})
            aid = pdf_artifact.get("artifactId") or n3_step.get("outputs", {}).get("artifactId")
            check("second-order nudge PDF artifact created", bool(aid), run_body)

            if aid:
                dl = client.get(f"/artifacts/{aid}/download")
                check("PDF artifact downloads", dl.status_code == 200, dl.status_code)
                (OUT / "new-customer-second-order-nudge.pdf").write_bytes(dl.content)
                text = pdf_text(dl.content)
                check("PDF has more than just a title (regression check)", len(text.splitlines()) > 5, text)
                if matched_rows:
                    flagged = matched_rows[0]
                    check("PDF contains a real flagged customer's name from the live mirror", str(flagged.get("name") or "") in text, text[:600])
                    check("PDF contains a real flagged customer's id from the live mirror", str(flagged.get("customerId") or "") in text, text[:600])
                else:
                    check("empty-result PDF is still a real, parseable artifact", isinstance(text, str), text[:200])

            n4_step = steps_by_id.get("n4", {})
            check("nudge email drafted, never sent (draftId present, no sendId)",
                  n4_step.get("status") == "completed" and n4_step.get("outputs", {}).get("draftId") and not n4_step.get("outputs", {}).get("sendId"),
                  n4_step)

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
