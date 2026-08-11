"""End-to-end test of Win-Back Radar as a real, user-buildable "My Workflow"
graph -- the thing this session graduated from _smoke/test_winback_radar_live.py's
hand-written Python proof into an actual publishable workflow.

Same harness/rationale as test_winback_radar_live.py (real mcp-minierp against
the live-mirrored ERP, real mcp-office/mcp-email, real _govern pipeline) --
the difference is the business logic (region lookup -> recency check -> flag
overdue accounts -> PDF -> draft email) now lives entirely INSIDE the graph
(one new bulk tool + one filter node), not in this test's own Python:

  trigger (country/state/city, win_back_days)
    -> tool_call: get_customer_order_recency (country/state/city <- trigger)
    -> filter: input <- "customers"; lastOrderDate older_than_days <- win_back_days
    -> tool_call: create_pdf_packet (tables <- filter's "matchedTable")
    -> tool_call: create_email_draft (never sent -- same "draft only" bonus step
       test_winback_radar_live.py already proves)

The graph is built with the EXACT node/edge JSON shape a human produces by
clicking through the My Workflow page (Add a step -> tool/Filter -> bind ->
Save/Check/Publish) -- see gateway/frontend/src/components/workflow/StepModal.tsx
and StepConfigFields.tsx's `filter` branch. Nothing here is a shortcut only
available to a script.

win_back_days is deliberately tiny (1 day) -- the live mirror's real data
distribution isn't known ahead of time, so a threshold that flags "ordered
more than a day ago" is the honest way to prove the mechanism end-to-end
without assuming what the real accounts' order history looks like.
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
TMP = ROOT / "_smoke" / ".tmp" / "winback-radar-graph"
OUT = ROOT / "_smoke" / ".tmp" / "winback-radar-graph-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "winback_graph_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "winback_graph_password",
    "GOVERNANCE_SESSION_SECRET": "winback-radar-graph-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18451/mcp",
    "OFFICE_MCP_URL": "http://127.0.0.1:18452/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18453/mcp",
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
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18451", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18452", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18453", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18451/health")
    wait_health("http://127.0.0.1:18452/health")
    wait_health("http://127.0.0.1:18453/health")
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
        consumer_id="user:winback_graph_builder", name="winback_graph_builder", key_hash="", status="active",
        role="user", type="user", categories=["analytics", "office", "email_draft"],
        login_password_hash=hash_password("builder_password"),
    ))

    def try_tool(client, tool, args, customer_id=""):
        return client.post("/dashboard/try-tool", json={"tool": tool, "args": args, "customer_id": customer_id})

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "winback_graph_builder", "password": "builder_password"})

        # ── Find a territory with real order history, same discovery approach
        #    test_winback_radar_live.py already uses for get_customers_by_region --
        #    get_customer_order_recency shares the exact same address/baccount
        #    resolution, so whichever candidate worked there should work here. ──
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
            if any(c.get("lastOrderDate") for c in customers):
                territory = kwargs
                break
        check("found a territory with real order-history data via get_customer_order_recency", territory is not None, tried)

        if territory is None:
            print("Could not reach a territory with order history on the live ERP mirror from this "
                  "environment (see _smoke/test_winback_radar_mock.py's note about WAF/network-level "
                  "403s blocking this sandbox) -- the filter-node mechanics themselves are already "
                  "proven network-independently by _smoke/test_filter_node.py.")
        else:
            # ── Build the SAME graph a human builds by hand on My Workflow ──
            graph = client.post("/workflow-graphs", json={
                "displayName": "Win-Back Radar",
                "description": "Flags customers overdue to reorder in a territory and drafts a digest for account management.",
                "nodes": [
                    # country/state/city are each optional: get_customer_order_recency only
                    # requires at least ONE of the three (enforced by the tool itself, not
                    # any one of them specifically) -- marking all three optional here is
                    # what lets a run supply just country and leave state/city blank.
                    {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": [
                        {"name": "country", "label": "Country", "optional": True},
                        {"name": "state", "label": "State", "optional": True},
                        {"name": "city", "label": "City", "optional": True},
                        {"name": "win_back_days", "label": "Win-back threshold (days)"},
                    ]}},
                    {"nodeId": "n1", "kind": "tool_call", "tool": "get_customer_order_recency",
                     "title": "Look up order recency by territory",
                     "inputBindings": {
                         "country": {"source": "trigger", "path": "country"},
                         "state": {"source": "trigger", "path": "state"},
                         "city": {"source": "trigger", "path": "city"},
                     },
                     "config": {"page": 1, "page_size": 25}},
                    {"nodeId": "n2", "kind": "filter", "title": "Flag reorder-overdue accounts",
                     "inputBindings": {"input": {"source": "node", "node_id": "n1", "path": "customers"}},
                     "config": {
                         "table_name": "Win-Back Radar",
                         "conditions": {"all": [
                             {"field": "lastOrderDate", "op": "older_than_days", "value": {"source": "trigger", "path": "win_back_days"}},
                         ]},
                     }},
                    {"nodeId": "n3", "kind": "tool_call", "tool": "create_pdf_packet",
                     "title": "Build win-back radar PDF",
                     "inputBindings": {"tables": {"source": "node", "node_id": "n2", "path": "matchedTable"}},
                     "config": {
                         "title": "Reorder-Due / Win-Back Radar",
                         "sections": [{"heading": "Reorder-Due / Win-Back Radar", "bullets": [
                             "Flags customers who haven't reordered recently in this territory.",
                             "See the attached table for the full list of flagged accounts.",
                         ]}],
                         "classification": ["INTERNAL"],
                     }},
                    {"nodeId": "n4", "kind": "tool_call", "tool": "create_email_draft",
                     "title": "Draft win-back digest email",
                     "config": {
                         "to": ["account-management@frontierdental.com"],
                         "subject": "Win-Back Radar: accounts flagged this week",
                         "body_markdown": "Hi team,\n\nThe attached Win-Back Radar report flags accounts that "
                                          "haven't reordered recently in this territory. Please review and "
                                          "follow up with the flagged accounts.\n\nRegards,\nGoverned AI Office Assistant",
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
            check("win-back radar graph created", graph.status_code == 201, graph.text)
            gid = graph.json()["graphId"]
            pub = client.post(f"/workflow-graphs/{gid}/publish", json={})
            check("win-back radar graph published", pub.status_code == 200, pub.text)

            run = client.post(f"/workflows/{gid}/run", json={**territory, "win_back_days": 1})
            run_body = run.json()
            check("win-back radar run completes", run.status_code == 201 and run_body.get("status") == "completed", run_body)
            steps_by_id = {s.get("stepId"): s for s in run_body.get("steps", [])}

            n1_out = steps_by_id.get("n1", {}).get("outputs", {})
            n2_out = steps_by_id.get("n2", {}).get("outputs", {})
            check("n1 (get_customer_order_recency) returned real customers", n1_out.get("status") == "ok" or bool(n1_out), n1_out)
            check("n2 (filter) flagged at least one reorder-overdue account", (n2_out.get("matchedCount") or 0) > 0, n2_out)

            # phone comes from a bulk bAccountId -> contact.phone1 join inside
            # get_customer_order_recency itself (best-effort: lowest-contactId
            # contact on file with a phone, since primaryContactId is
            # essentially unpopulated in this data -- see the tool's own
            # docstring) -- present here means it survived through the filter
            # untouched, no separate per-customer lookup step involved. None
            # is a legitimate real outcome for an account with no contact on
            # file that has a phone, so this only checks the KEY is present
            # on every row, and reports (not requires) a real value.
            n1_customers = n1_out.get("customers") or []
            check("every customer row from get_customer_order_recency carries a phone key",
                  bool(n1_customers) and all("phone" in c for c in n1_customers), n1_customers[:3])
            with_phone = [c for c in n1_customers if c.get("phone")]
            print(f"  info {len(with_phone)}/{len(n1_customers)} customers on this page have a real phone on file "
                  f"(None is expected for accounts with no contact on file that has one)")

            flagged = (n2_out.get("matched") or [{}])[0]
            check("the flagged win-back customer's own row also carries the phone key", "phone" in flagged, flagged)
            artifacts = run_body.get("artifacts") or []
            pdf_artifact = next((a for a in artifacts if a.get("artifactId")), {})
            aid = pdf_artifact.get("artifactId")
            check("win-back radar PDF artifact created", bool(aid), run_body)

            if aid:
                dl = client.get(f"/artifacts/{aid}/download")
                check("PDF artifact downloads", dl.status_code == 200, dl.status_code)
                (OUT / "winback-radar-graph.pdf").write_bytes(dl.content)
                text = pdf_text(dl.content)
                check("PDF contains a real flagged customer's name from the live mirror", str(flagged.get("name") or "") in text, text[:600])
                check("PDF contains a real flagged customer's id from the live mirror", str(flagged.get("customerId") or "") in text, text[:600])
                check("PDF has more than just a title (regression check)", len(text.splitlines()) > 5, text)

            n4_step = steps_by_id.get("n4", {})
            check("digest email drafted, never sent (draftId present, no sendId)",
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
