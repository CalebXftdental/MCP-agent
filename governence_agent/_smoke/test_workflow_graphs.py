"""Local HTTP smoke for user-buildable workflow graphs ("My Workflow").

Covers: CRUD + publish lifecycle, validation rejections (cycle, orphan, unknown
tool, missing category, send-risk without a gate), a full approval-gate ->
send-risk run, the generalized export-risk auto-pause on a NON-default tool
(create_word_report, proving the interpreter isn't hardcoded to
create_excel_report), a two-gate run exercising the multi-approval resume
correctness fix, and automation scheduling by graph id.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "workflow-graphs"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "graph_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "graph_password",
    "GOVERNANCE_SESSION_SECRET": "workflow-graphs-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "GOVERNANCE_BROAD_EXPORT_APPROVAL_ROWS": "1",
    "OFFICE_MCP_URL": "http://127.0.0.1:18091/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18092/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "10",
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


def wait_health(url: str, timeout: float = 10.0) -> None:
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


print("start office + email backends")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18091", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18092", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18091/health")
    wait_health("http://127.0.0.1:18092/health")
    check("office backend healthy", True)
    check("email backend healthy", True)

    sys.path.insert(0, str(ROOT / "gateway"))
    sys.path.insert(0, str(ROOT / "governance_core"))
    gateway_app = importlib.import_module("app")
    from auth.passwords import hash_password  # noqa: E402
    from store.models import ConsumerRecord  # noqa: E402

    store = gateway_app.get_store()
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:graph_builder", name="graph_builder", key_hash="", status="active",
        role="user", type="user", categories=["email_draft", "email_send_external", "office"],
        login_password_hash=hash_password("builder_password"),
    ))
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:no_office", name="no_office_user", key_hash="", status="active",
        role="user", type="user", categories=["email_draft"],
        login_password_hash=hash_password("no_office_password"),
    ))

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        admin_login = client.post("/dashboard/login", json={"username": "graph_admin", "password": "graph_password"})
        check("admin login succeeds", admin_login.status_code == 200, admin_login.text)

        builder_login = client.post("/dashboard/login", json={"username": "graph_builder", "password": "builder_password"})
        check("builder login succeeds", builder_login.status_code == 200, builder_login.text)

        # ── palette catalog ────────────────────────────────────────────────
        catalog = client.get("/dashboard/workflow-graph-catalog")
        catalog_tools = {t["canonical"]: t for t in catalog.json().get("tools", [])}
        check("catalog lists create_email_draft", catalog.status_code == 200 and "create_email_draft" in catalog_tools, catalog.json())
        check("catalog marks granted tool", catalog_tools.get("create_email_draft", {}).get("granted") is True)
        check("catalog marks ungranted tool", catalog_tools.get("get_gl_account_transactions", {}).get("granted") is False, catalog_tools.get("get_gl_account_transactions"))
        check("catalog exposes risk level", catalog_tools.get("send_email_draft", {}).get("riskLevel") == "send", catalog_tools.get("send_email_draft"))

        # ── CRUD + publish lifecycle ────────────────────────────────────────
        minimal = client.post("/workflow-graphs", json={
            "displayName": "Minimal draft",
            "nodes": [{"nodeId": "t1", "kind": "trigger"}, {"nodeId": "n1", "kind": "tool_call", "tool": "create_email_draft",
                       "config": {"to": ["teammate@example.com"], "subject": "Hi", "body_markdown": "hello", "classification": ["INTERNAL"]}}],
            "edges": [{"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}],
        })
        check("create graph succeeds", minimal.status_code == 201, minimal.text)
        gid = minimal.json()["graphId"]
        check("new graph is draft", minimal.json().get("status") == "draft")

        fetched = client.get(f"/workflow-graphs/{gid}")
        check("get graph round-trips nodes", fetched.status_code == 200 and len(fetched.json().get("nodes") or []) == 2, fetched.text)

        listed = client.get("/workflow-graphs")
        check("list graphs includes new graph", any(g.get("graphId") == gid for g in listed.json().get("graphs", [])))

        versioned = client.post(f"/workflow-graphs/{gid}/versions", json={"nodes": [], "edges": []})
        check("version with no trigger is rejected", versioned.status_code == 400, versioned.text)

        published = client.post(f"/workflow-graphs/{gid}/publish", json={})
        check("publish succeeds", published.status_code == 200 and published.json().get("status") == "active", published.text)
        check("published version is 1", published.json().get("publishedVersion") == 1)

        in_catalog = client.get("/workflows")
        check("published graph appears in /workflows", any(t.get("templateId") == gid for t in in_catalog.json().get("workflows", in_catalog.json().get("templates", []))), in_catalog.json())

        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "graph_admin", "password": "graph_password"})
        disabled = client.post(f"/admin/workflows/{gid}/disable", json={"reason": "smoke"})
        check("admin can disable a graph-backed template", disabled.status_code == 200 and disabled.json().get("status") == "disabled", disabled.text)
        client.post(f"/admin/workflows/{gid}/enable", json={})
        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "graph_builder", "password": "builder_password"})

        # ── validation rejections ───────────────────────────────────────────
        cycle = client.post("/workflow-graphs", json={
            "displayName": "Cycle",
            "nodes": [{"nodeId": "t1", "kind": "trigger"}, {"nodeId": "n1", "kind": "tool_call", "tool": "create_email_draft", "config": {}},
                      {"nodeId": "n2", "kind": "tool_call", "tool": "create_email_draft", "config": {}}],
            "edges": [{"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}, {"edgeId": "e2", "sourceNodeId": "n1", "targetNodeId": "n2"},
                      {"edgeId": "e3", "sourceNodeId": "n2", "targetNodeId": "n1"}],
        })
        check("cycle is rejected", cycle.status_code == 400 and "cycle" in cycle.json().get("error", ""), cycle.text)

        orphan = client.post("/workflow-graphs", json={
            "displayName": "Orphan",
            "nodes": [{"nodeId": "t1", "kind": "trigger"}, {"nodeId": "n1", "kind": "tool_call", "tool": "create_email_draft", "config": {}},
                      {"nodeId": "n2", "kind": "tool_call", "tool": "create_email_draft", "config": {}}],
            "edges": [{"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}],
        })
        check("orphan node is rejected", orphan.status_code == 400 and "not reachable" in orphan.json().get("error", ""), orphan.text)

        unknown_tool = client.post("/workflow-graphs", json={
            "displayName": "Unknown tool",
            "nodes": [{"nodeId": "t1", "kind": "trigger"}, {"nodeId": "n1", "kind": "tool_call", "tool": "not_a_real_tool", "config": {}}],
            "edges": [{"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}],
        })
        check("unknown tool is rejected", unknown_tool.status_code == 400 and "unknown tool" in unknown_tool.json().get("error", ""), unknown_tool.text)

        send_no_gate = client.post("/workflow-graphs", json={
            "displayName": "Send without gate",
            "nodes": [{"nodeId": "t1", "kind": "trigger"}, {"nodeId": "n1", "kind": "tool_call", "tool": "create_email_draft", "config": {}},
                      {"nodeId": "n2", "kind": "tool_call", "tool": "send_email_draft", "config": {}}],
            "edges": [{"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}, {"edgeId": "e2", "sourceNodeId": "n1", "targetNodeId": "n2"}],
        })
        check("send-risk node without an approval gate is rejected", send_no_gate.status_code == 400 and "approval gate" in send_no_gate.json().get("error", ""), send_no_gate.text)

        client.post("/dashboard/logout")
        no_office_login = client.post("/dashboard/login", json={"username": "no_office_user", "password": "no_office_password"})
        missing_category = client.post("/workflow-graphs", json={
            "displayName": "No office access",
            "nodes": [{"nodeId": "t1", "kind": "trigger"}, {"nodeId": "n1", "kind": "tool_call", "tool": "create_excel_report", "config": {}}],
            "edges": [{"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}],
        })
        check("tool the owner lacks access to is rejected", missing_category.status_code == 400 and "you do not have access" in missing_category.json().get("error", ""), missing_category.text)
        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "graph_builder", "password": "builder_password"})

        # ── Graph A: approval_gate directly before a send-risk node ─────────
        graph_a = client.post("/workflow-graphs", json={
            "displayName": "Draft then send",
            "nodes": [
                {"nodeId": "t1", "kind": "trigger"},
                {"nodeId": "n1", "kind": "tool_call", "tool": "create_email_draft",
                 "config": {"to": ["teammate@example.com"], "subject": "Follow-up", "body_markdown": "hello", "classification": ["INTERNAL"]}},
                {"nodeId": "g1", "kind": "approval_gate", "config": {"reason": "Review before sending", "risk_level": "medium"}},
                {"nodeId": "n2", "kind": "tool_call", "tool": "send_email_draft",
                 "inputBindings": {"draft_id": {"source": "node", "node_id": "n1", "path": "draftId"},
                                    "approval_id": {"source": "node", "node_id": "g1", "path": "approvalId"}}},
            ],
            "edges": [
                {"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"},
                {"edgeId": "e2", "sourceNodeId": "n1", "targetNodeId": "g1"},
                {"edgeId": "e3", "sourceNodeId": "g1", "targetNodeId": "n2"},
            ],
        })
        check("graph A created", graph_a.status_code == 201, graph_a.text)
        gid_a = graph_a.json()["graphId"]
        client.post(f"/workflow-graphs/{gid_a}/publish", json={})

        run_a = client.post(f"/workflows/{gid_a}/run", json={})
        run_a_body = run_a.json()
        check("graph A run pauses at the gate", run_a.status_code == 201 and run_a_body.get("status") == "approval_required", run_a_body)
        gate_step = next((s for s in run_a_body.get("steps", []) if s.get("stepId") == "g1"), {})
        check("gate step is pending", gate_step.get("status") == "pending", gate_step)
        n2_step_before = next((s for s in run_a_body.get("steps", []) if s.get("stepId") == "n2"), None)
        check("send node has not run yet", n2_step_before is None, run_a_body)

        appr_a = run_a_body.get("approval", {}).get("approvalId")
        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "graph_admin", "password": "graph_password"})
        approve_a = client.post(f"/approvals/{appr_a}/approve", json={"note": "ok"})
        check("admin approves graph A's gate", approve_a.status_code == 200, approve_a.text)
        resumed_a = client.post(f"/workflow-runs/{run_a_body['runId']}/resume", json={"approval_id": appr_a})
        resumed_a_body = resumed_a.json()
        check("graph A resume completes the run", resumed_a.status_code == 200 and resumed_a_body.get("status") == "completed", resumed_a_body)
        n2_step_after = next((s for s in resumed_a_body.get("steps", []) if s.get("stepId") == "n2"), {})
        check("send node actually ran after resume", n2_step_after.get("status") == "completed" and n2_step_after.get("outputs", {}).get("sendId"), n2_step_after)
        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "graph_builder", "password": "builder_password"})

        # ── Graph B: generalized export-risk auto-pause on a NON-default tool ─
        big_rows = [{"row": i} for i in range(5)]
        graph_b = client.post("/workflow-graphs", json={
            "displayName": "Word export",
            "nodes": [
                {"nodeId": "t1", "kind": "trigger"},
                {"nodeId": "n1", "kind": "tool_call", "tool": "create_word_report",
                 "config": {"title": "Report", "sections": [], "tables": [{"name": "Data", "rows": big_rows}], "classification": ["PII", "SENSITIVE"]}},
            ],
            "edges": [{"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}],
        })
        gid_b = graph_b.json()["graphId"]
        client.post(f"/workflow-graphs/{gid_b}/publish", json={})
        run_b = client.post(f"/workflows/{gid_b}/run", json={})
        run_b_body = run_b.json()
        check("export-risk auto-pause fires for create_word_report (not just create_excel_report)", run_b.status_code == 201 and run_b_body.get("status") == "approval_required", run_b_body)
        export_step = next((s for s in run_b_body.get("steps", []) if s.get("stepId") == "n1__export_approval"), None)
        check("synthetic export-approval step uses the per-node id, not the hardcoded default", export_step is not None and export_step.get("status") == "pending", run_b_body)
        appr_b = run_b_body.get("approval", {}).get("approvalId")
        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "graph_admin", "password": "graph_password"})
        client.post(f"/approvals/{appr_b}/approve", json={"note": "ok"})
        resumed_b = client.post(f"/workflow-runs/{run_b_body['runId']}/resume", json={"approval_id": appr_b})
        check("graph B resume completes", resumed_b.status_code == 200 and resumed_b.json().get("status") == "completed", resumed_b.text)
        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "graph_builder", "password": "builder_password"})

        # ── Graph C: two sequential gates -- multi-approval resume correctness ─
        graph_c = client.post("/workflow-graphs", json={
            "displayName": "Two gates",
            "nodes": [
                {"nodeId": "t1", "kind": "trigger"},
                {"nodeId": "n1", "kind": "tool_call", "tool": "create_email_draft",
                 "config": {"to": ["teammate@example.com"], "subject": "S", "body_markdown": "B", "classification": ["INTERNAL"]}},
                {"nodeId": "g1", "kind": "approval_gate", "config": {"reason": "gate before send"}},
                {"nodeId": "n2", "kind": "tool_call", "tool": "send_email_draft",
                 "inputBindings": {"draft_id": {"source": "node", "node_id": "n1", "path": "draftId"},
                                    "approval_id": {"source": "node", "node_id": "g1", "path": "approvalId"}}},
                {"nodeId": "n3", "kind": "tool_call", "tool": "create_excel_report",
                 "config": {"title": "Report", "tables": [{"name": "Data", "rows": big_rows}], "classification": ["PII", "SENSITIVE"]}},
            ],
            "edges": [
                {"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"},
                {"edgeId": "e2", "sourceNodeId": "n1", "targetNodeId": "g1"},
                {"edgeId": "e3", "sourceNodeId": "g1", "targetNodeId": "n2"},
                {"edgeId": "e4", "sourceNodeId": "n2", "targetNodeId": "n3"},
            ],
        })
        check("graph C created", graph_c.status_code == 201, graph_c.text)
        gid_c = graph_c.json()["graphId"]
        client.post(f"/workflow-graphs/{gid_c}/publish", json={})

        run_c = client.post(f"/workflows/{gid_c}/run", json={})
        run_c_body = run_c.json()
        check("graph C pauses at first gate", run_c.status_code == 201 and run_c_body.get("status") == "approval_required", run_c_body)
        appr_c1 = run_c_body.get("approval", {}).get("approvalId")
        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "graph_admin", "password": "graph_password"})
        client.post(f"/approvals/{appr_c1}/approve", json={"note": "gate 1 ok"})
        resumed_c1 = client.post(f"/workflow-runs/{run_c_body['runId']}/resume", json={"approval_id": appr_c1})
        resumed_c1_body = resumed_c1.json()
        check("graph C proceeds past gate 1 and pauses again at the export gate", resumed_c1.status_code == 200 and resumed_c1_body.get("status") == "approval_required", resumed_c1_body)
        n2_ran = next((s for s in resumed_c1_body.get("steps", []) if s.get("stepId") == "n2"), {})
        check("send node ran between the two gates", n2_ran.get("status") == "completed" and n2_ran.get("outputs", {}).get("sendId"), n2_ran)

        # This is the actual correctness-fix assertion: appr_c1 is already "approved"
        # (stale) but the run's CURRENTLY blocking approval is the new export gate,
        # still pending -- resuming with no explicit approval_id must NOT match the
        # stale approved one and must correctly report "still pending".
        premature_resume = client.post(f"/workflow-runs/{run_c_body['runId']}/resume", json={})
        check("resume without approval_id does not match the stale first approval", premature_resume.status_code == 409 and "pending" in premature_resume.json().get("error", ""), premature_resume.text)

        appr_c2 = resumed_c1_body.get("approval", {}).get("approvalId")
        check("second approval id differs from the first", appr_c2 and appr_c2 != appr_c1, (appr_c1, appr_c2))
        client.post(f"/approvals/{appr_c2}/approve", json={"note": "export gate ok"})
        resumed_c2 = client.post(f"/workflow-runs/{run_c_body['runId']}/resume", json={"approval_id": appr_c2})
        check("graph C fully completes after both gates approved", resumed_c2.status_code == 200 and resumed_c2.json().get("status") == "completed", resumed_c2.text)
        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "graph_builder", "password": "builder_password"})

        # ── Automation scheduling by graph id (no automation_store changes needed) ─
        graph_d = client.post("/workflow-graphs", json={
            "displayName": "Scheduled draft",
            "nodes": [{"nodeId": "t1", "kind": "trigger"},
                      {"nodeId": "n1", "kind": "tool_call", "tool": "create_email_draft",
                       "config": {"to": ["teammate@example.com"], "subject": "Auto", "body_markdown": "hi", "classification": ["INTERNAL"]}}],
            "edges": [{"edgeId": "e1", "sourceNodeId": "t1", "targetNodeId": "n1"}],
        })
        gid_d = graph_d.json()["graphId"]
        client.post(f"/workflow-graphs/{gid_d}/publish", json={})
        now = int(time.time())
        scheduled = client.post("/automations", json={
            "template_id": gid_d, "display_name": "Scheduled graph draft", "interval_sec": 3600, "next_run_at": now - 1, "inputs": {},
        })
        check("automation can schedule a graph-backed template", scheduled.status_code == 201, scheduled.text)
        client.post("/dashboard/logout")
        client.post("/dashboard/login", json={"username": "graph_admin", "password": "graph_password"})
        due = client.post("/automations/run-due", json={"now": now})
        due_body = due.json()
        result = (due_body.get("results") or [{}])[0]
        check("scheduled graph run executes", due.status_code == 200 and result.get("status") == "completed", due_body)
finally:
    office.terminate()
    email.terminate()
    for proc in (office, email):
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
