"""End-to-end test of "Unreachable-Account Flag" as a real, user-buildable "My
Workflow" graph -- same recipe as test_winback_radar_graph.py /
test_ar_credit_hold_radar_graph.py:

  trigger (country/state/city)
    -> tool_call: get_customer_order_recency (country/state/city <- trigger)
    -> filter: input <- "customers"; phone is null or empty (op "in", value [None, ""] --
       FILTER_OPS' eq/ne/gt/gte/lt/lte all short-circuit to False whenever row_value is
       None, per gateway/workflow_graph_interpreter.py's _evaluate_condition, so "in" is
       the only op that can actually match a null phone; empty string is included too
       since the source field could in principle come back as "" rather than None)
    -> tool_call: create_excel_report (tables <- filter's "matchedTable")

`phone` is confirmed a real field on get_customer_order_recency's output
(mcp-minierp/sqlagent/analytics.py's per-customer rollup: best-effort lowest-
contactId contact on file with a phone1, None when no contact on file has one --
see the tool's own docstring). This is the simplest of the three new workflows
(no join/loop node), used as the confidence check on the graph-building API
recipe before the join-based Big-Spender Drop-Off and the loop-based
Declining-SKU Alert.
"""
from __future__ import annotations

import importlib
import io
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "unreachable-account-flag-graph"
OUT = ROOT / "_smoke" / ".tmp" / "unreachable-account-flag-graph-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "unreachable_flag_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "unreachable_flag_password",
    "GOVERNANCE_SESSION_SECRET": "unreachable-account-flag-graph-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18571/mcp",
    "OFFICE_MCP_URL": "http://127.0.0.1:18572/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18573/mcp",
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


_NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def xlsx_cell_texts(payload: bytes) -> list[str]:
    out: list[str] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("s:si", _NS):
                shared.append("".join(t.text or "" for t in si.findall(".//s:t", _NS)))
        for name in zf.namelist():
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                root = ET.fromstring(zf.read(name))
                for c in root.findall(".//s:c", _NS):
                    inline = c.find("s:is/s:t", _NS)
                    if inline is not None:
                        out.append(inline.text or "")
                        continue
                    v = c.find("s:v", _NS)
                    text = v.text if v is not None else None
                    if text is None:
                        continue
                    if c.get("t") == "s":
                        try:
                            out.append(shared[int(text)])
                        except (ValueError, IndexError):
                            out.append(text)
                    else:
                        out.append(text)
    return out


print("start mcp-minierp (real ERP mirror) + mcp-office + mcp-email")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
minierp = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18571", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18572", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18573", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18571/health")
    wait_health("http://127.0.0.1:18572/health")
    wait_health("http://127.0.0.1:18573/health")
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
        consumer_id="user:unreachable_flag_builder", name="unreachable_flag_builder", key_hash="", status="active",
        role="user", type="user", categories=["analytics", "office"],
        login_password_hash=hash_password("builder_password"),
    ))

    def try_tool(client, tool, args, customer_id=""):
        return client.post("/dashboard/try-tool", json={"tool": tool, "args": args, "customer_id": customer_id})

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "unreachable_flag_builder", "password": "builder_password"})

        # ── Find a territory with real customer data (same discovery approach
        #    the win-back radar / AR credit-hold reference tests use). ──
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
            if customers:
                territory = kwargs
                break
        check("found a territory with real customer data via get_customer_order_recency", territory is not None, tried)

        if territory is None:
            print("Could not reach a territory with data on the live ERP mirror from this environment.")
        else:
            graph = client.post("/workflow-graphs", json={
                "displayName": "Unreachable-Account Flag",
                "description": "Flags customers in a territory who have no phone number on file for any contact "
                                "(no way to reach them by phone) and exports the list to a workbook.",
                "nodes": [
                    {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": [
                        {"name": "country", "label": "Country", "optional": True},
                        {"name": "state", "label": "State", "optional": True},
                        {"name": "city", "label": "City", "optional": True},
                    ]}},
                    {"nodeId": "n1", "kind": "tool_call", "tool": "get_customer_order_recency",
                     "title": "Look up order recency by territory",
                     "inputBindings": {
                         "country": {"source": "trigger", "path": "country"},
                         "state": {"source": "trigger", "path": "state"},
                         "city": {"source": "trigger", "path": "city"},
                     },
                     "config": {"page": 1, "page_size": 25}},
                    {"nodeId": "n2", "kind": "filter", "title": "Flag accounts with no phone on file",
                     "inputBindings": {"input": {"source": "node", "node_id": "n1", "path": "customers"}},
                     "config": {
                         "table_name": "Unreachable Accounts (No Phone on File)",
                         # eq/ne/... all short-circuit to False when row_value is None (see
                         # _evaluate_condition) -- "in" is the op that actually matches null,
                         # and covers a real "" too.
                         "conditions": {"all": [
                             {"field": "phone", "op": "in", "value": [None, ""]},
                         ]},
                     }},
                    {"nodeId": "n3", "kind": "tool_call", "tool": "create_excel_report",
                     "title": "Build unreachable-account workbook",
                     "inputBindings": {"tables": {"source": "node", "node_id": "n2", "path": "matchedTable"}},
                     "config": {"title": "Unreachable-Account Flag", "classification": ["INTERNAL", "PII"]}},
                ],
                "edges": [
                    {"edgeId": "e1", "sourceNodeId": "trigger", "targetNodeId": "n1"},
                    {"edgeId": "e2", "sourceNodeId": "n1", "targetNodeId": "n2"},
                    {"edgeId": "e3", "sourceNodeId": "n2", "targetNodeId": "n3"},
                ],
            })
            check("unreachable-account flag graph created", graph.status_code == 201, graph.text)
            gid = graph.json()["graphId"]
            pub = client.post(f"/workflow-graphs/{gid}/publish", json={})
            check("unreachable-account flag graph published", pub.status_code == 200, pub.text)

            run = client.post(f"/workflows/{gid}/run", json={**territory})
            run_body = run.json()
            check("unreachable-account flag run reaches the workbook step", run.status_code in (201, 409), run_body)
            steps_by_id = {s.get("stepId"): s for s in run_body.get("steps", [])}

            n1_out = steps_by_id.get("n1", {}).get("outputs", {})
            check("n1 (get_customer_order_recency) returned real customers", bool(n1_out.get("customers")), n1_out)
            n1_customers = n1_out.get("customers") or []
            check("every customer row carries a phone key", bool(n1_customers) and all("phone" in c for c in n1_customers), n1_customers[:3])

            n2_out = steps_by_id.get("n2", {}).get("outputs", {})
            matched = n2_out.get("matchedCount") or 0
            no_phone_real = [c for c in n1_customers if not c.get("phone")]
            print(f"  info matched={matched} accounts with no phone on file out of {len(n1_customers)} scanned "
                  f"({len(no_phone_real)} counted directly from n1's raw output, should agree)")
            check("filter's matchedCount agrees with a direct recount of n1's raw customers",
                  matched == len(no_phone_real), (matched, len(no_phone_real)))
            check("filter step ran to completion over the real dataset (matchedCount is a real int, not an error)",
                  isinstance(matched, int) and matched >= 0, n2_out)
            matched_rows = n2_out.get("matched") or []
            check("every flagged row genuinely has no phone (regression check)",
                  all(not row.get("phone") for row in matched_rows), matched_rows[:5])
            check("no phone-having row leaked into the flagged set",
                  not any((row.get("phone") for row in matched_rows)), matched_rows[:5])

            if run_body.get("status") == "approval_required":
                approval_id = (run_body.get("approval") or {}).get("approvalId")
                check("broad-export approval was actually created", bool(approval_id), run_body)
                client.post("/dashboard/logout")
                client.post("/dashboard/login", json={"username": "unreachable_flag_admin", "password": "unreachable_flag_password"})
                approve = client.post(f"/approvals/{approval_id}/approve", json={"note": "reviewed unreachable-account workbook"})
                check("admin approves the broad export", approve.status_code == 200, approve.text)
                resumed = client.post(f"/workflow-runs/{run_body['runId']}/resume", json={"approval_id": approval_id})
                run_body = resumed.json()
                check("resume completes the run", resumed.status_code == 200 and run_body.get("status") == "completed", run_body)
                client.post("/dashboard/logout")
                client.post("/dashboard/login", json={"username": "unreachable_flag_builder", "password": "builder_password"})
            else:
                check("unreachable-account flag run completes without needing approval", run_body.get("status") == "completed", run_body)

            n3_step = next((s for s in run_body.get("steps", []) if s.get("stepId") == "n3"), {})
            aid = n3_step.get("outputs", {}).get("artifactId")
            check("unreachable-account flag workbook artifact created", bool(aid), n3_step)

            if aid:
                dl = client.get(f"/artifacts/{aid}/download")
                check("workbook artifact downloads", dl.status_code == 200, dl.status_code)
                (OUT / "unreachable-account-flag.xlsx").write_bytes(dl.content)
                cells = xlsx_cell_texts(dl.content)
                if matched_rows:
                    check("workbook has more than a couple cells (regression check)", len(cells) > 5, len(cells))
                    real_customer_id = str(matched_rows[0].get("customerId") or "")
                    check("workbook contains a real flagged customer id from the live mirror",
                          any(real_customer_id == c or (real_customer_id and real_customer_id in c) for c in cells), cells[:20])
                else:
                    check("empty-result workbook is still a valid, parseable xlsx", isinstance(cells, list), cells)

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
