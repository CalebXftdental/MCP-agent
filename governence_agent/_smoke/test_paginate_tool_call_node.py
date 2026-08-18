"""Smoke test for the generic `paginate` capability on `tool_call` nodes
("My Workflow" -- closing finalize_stage_1.md section 3.2a, the paginate-
until-exhausted gap).

Part 1 (network-independent, no server): exercises
gateway/workflow_graph_interpreter.py's `_exhaust_tool_call` directly with a
stubbed `govern`/`parse` pair -- it's already dependency-injected for exactly
this, so there's no need to stand up a real paginating MCP tool to prove the
loop mechanics (full exhaustion, the max_pages cap, the max_duration_sec cap,
the safe no-op for a tool with no hasMore-shaped output, and a mid-loop
failure preserving partial results with a clear `truncatedReason`).

Part 2 (full HTTP stack, same bootstrap as test_filter_node.py): proves
validate_graph's build-time guard on a bad `max_pages`/`max_duration_sec`
fires at graph-create time, and a well-formed paginate config is accepted --
using create_pdf_packet as the tool_call target (any tool works; paginate
eligibility has no allowlist, see workflow_graph_store.py's own comment).
"""
from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "paginate-node"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "paginate_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "paginate_password",
    "GOVERNANCE_SESSION_SECRET": "paginate-node-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18451/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18452/mcp",
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


def identity_parse(raw):
    return raw


sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))
gateway_app = importlib.import_module("app")
import workflow_graph_interpreter as wgi  # noqa: E402

# ── Part 1: _exhaust_tool_call mechanics, no server needed ──────────────────

print("Part 1: _exhaust_tool_call mechanics (stubbed govern/parse)")


async def _govern_three_pages(tool, session_id, customer_id, args):
    pages = [
        {"status": "ok", "customers": ["a", "b"], "hasMore": True},
        {"status": "ok", "customers": ["c", "d"], "hasMore": True},
        {"status": "ok", "customers": ["e"], "hasMore": False},
    ]
    return pages[args["page"] - 1]


result = asyncio.run(wgi._exhaust_tool_call(
    "fake_tool", {"page": 1}, "sess", "", _govern_three_pages, identity_parse,
    max_pages=20, max_duration_sec=90,
))
check("full exhaustion merges all pages' rows in order", result.get("customers") == ["a", "b", "c", "d", "e"], result)
check("full exhaustion: truncated is False", result.get("truncated") is False, result)
check("full exhaustion: no truncatedReason on clean stop", "truncatedReason" not in result, result)
check("full exhaustion: pagesFetched == 3", result.get("pagesFetched") == 3, result)
check("full exhaustion: count reflects the merged length, not the last page's", result.get("count") == 5, result)


async def _govern_infinite(tool, session_id, customer_id, args):
    return {"status": "ok", "rows": [args["page"]], "hasMore": True}


result = asyncio.run(wgi._exhaust_tool_call(
    "fake_tool", {"page": 1}, "sess", "", _govern_infinite, identity_parse,
    max_pages=3, max_duration_sec=90,
))
check("max_pages cap stops the loop at exactly the cap", result.get("pagesFetched") == 3, result)
check("max_pages cap: truncated is True", result.get("truncated") is True, result)
check("max_pages cap: truncatedReason is 'max_pages'", result.get("truncatedReason") == "max_pages", result)
check("max_pages cap: rows merged up to the cap, nothing more", result.get("rows") == [1, 2, 3], result)


async def _govern_slow(tool, session_id, customer_id, args):
    await asyncio.sleep(0.05)
    return {"status": "ok", "rows": [args["page"]], "hasMore": True}


result = asyncio.run(wgi._exhaust_tool_call(
    "fake_tool", {"page": 1}, "sess", "", _govern_slow, identity_parse,
    max_pages=1000, max_duration_sec=0.12,
))
check("max_duration_sec cap fires before max_pages ever would", result.get("truncatedReason") == "max_duration", result)
check("max_duration_sec cap: only a handful of pages fetched, not 1000", 0 < result.get("pagesFetched", 0) < 10, result)


async def _govern_single_entity(tool, session_id, customer_id, args):
    return {"status": "ok", "customerId": "C1", "name": "Acme"}


result = asyncio.run(wgi._exhaust_tool_call(
    "fake_tool", {"page": 1}, "sess", "", _govern_single_entity, identity_parse,
    max_pages=20, max_duration_sec=90,
))
check(
    "a tool with no list-valued field at all is a safe no-op (returned unchanged, one call)",
    result == {"status": "ok", "customerId": "C1", "name": "Acme"}, result,
)


async def _govern_list_no_hasmore(tool, session_id, customer_id, args):
    return {"status": "ok", "lineItems": [1, 2, 3]}


result = asyncio.run(wgi._exhaust_tool_call(
    "fake_tool", {"page": 1}, "sess", "", _govern_list_no_hasmore, identity_parse,
    max_pages=20, max_duration_sec=90,
))
check(
    "a tool with a row list but no hasMore key stops after one page, not truncated",
    result.get("lineItems") == [1, 2, 3] and result.get("truncated") is False and result.get("pagesFetched") == 1, result,
)


async def _govern_fails_first_page(tool, session_id, customer_id, args):
    return {"status": "error", "intent": "x", "errorCode": "ReadTimeout", "message": "boom"}


result = asyncio.run(wgi._exhaust_tool_call(
    "fake_tool", {"page": 1}, "sess", "", _govern_fails_first_page, identity_parse,
    max_pages=20, max_duration_sec=90,
))
check(
    "a page-1 failure is returned unchanged so the normal _FAILURE_STATUSES path still fails the step",
    result.get("status") == "error", result,
)


async def _govern_fails_second_page(tool, session_id, customer_id, args):
    if args["page"] == 1:
        return {"status": "ok", "customers": ["a"], "hasMore": True}
    return {"status": "error", "errorCode": "ReadTimeout"}


result = asyncio.run(wgi._exhaust_tool_call(
    "fake_tool", {"page": 1}, "sess", "", _govern_fails_second_page, identity_parse,
    max_pages=20, max_duration_sec=90,
))
check("mid-loop failure preserves the prior successful page's rows", result.get("customers") == ["a"], result)
check("mid-loop failure: truncated is True", result.get("truncated") is True, result)
check("mid-loop failure: truncatedReason is 'request_error'", result.get("truncatedReason") == "request_error", result)
check("mid-loop failure: pagesFetched counts only the successful pages", result.get("pagesFetched") == 1, result)

# ── Part 2: validate_graph's build-time guard on paginate config ────────────

print("\nPart 2: validate_graph paginate-config guard (full HTTP stack)")

print("start office + email backends")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18451", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18452", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    from starlette.testclient import TestClient

    wait_health("http://127.0.0.1:18451/health")
    wait_health("http://127.0.0.1:18452/health")
    check("office backend healthy", True)
    check("email backend healthy", True)

    from store import get_store  # noqa: E402
    from store.models import ConsumerRecord  # noqa: E402
    from auth.passwords import hash_password  # noqa: E402

    store = get_store()
    store.upsert_consumer(ConsumerRecord(
        consumer_id="user:paginate_builder", name="paginate_builder", key_hash="", status="active",
        role="user", type="user", categories=["office", "email_draft"],
        login_password_hash=hash_password("builder_password"),
    ))

    def graph_payload(paginate_config: dict) -> dict:
        return {
            "displayName": "Paginate Config Smoke Test",
            "nodes": [
                {"nodeId": "trigger", "kind": "trigger", "config": {"inputs": []}},
                {"nodeId": "n_pdf", "kind": "tool_call", "tool": "create_pdf_packet",
                 "config": {"title": "Paginate Config Smoke Test", **paginate_config}},
            ],
            "edges": [{"edgeId": "e_pdf", "sourceNodeId": "trigger", "targetNodeId": "n_pdf"}],
        }

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "paginate_builder", "password": "builder_password"})

        bad_max_pages = client.post("/workflow-graphs", json=graph_payload({"paginate": True, "max_pages": -1}))
        check("negative max_pages is rejected at create time", bad_max_pages.status_code == 400, bad_max_pages.text)

        bad_max_duration = client.post("/workflow-graphs", json=graph_payload({"paginate": True, "max_duration_sec": 0}))
        check("zero max_duration_sec is rejected at create time", bad_max_duration.status_code == 400, bad_max_duration.text)

        bad_type = client.post("/workflow-graphs", json=graph_payload({"paginate": True, "max_pages": "lots"}))
        check("non-numeric max_pages is rejected at create time", bad_type.status_code == 400, bad_type.text)

        good = client.post("/workflow-graphs", json=graph_payload({"paginate": True, "max_pages": 5, "max_duration_sec": 30}))
        check("a well-formed paginate config is accepted at create time", good.status_code == 201, good.text)

        no_paginate_bad_value = client.post("/workflow-graphs", json=graph_payload({"max_pages": -1}))
        check(
            "max_pages is only validated when paginate is actually set (not a stray literal arg)",
            no_paginate_bad_value.status_code == 201, no_paginate_bad_value.text,
        )
finally:
    office.terminate()
    email.terminate()
    for proc in (office, email):
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
