"""LIVE test of use case #2 (Reorder-Due / Win-Back Radar) against the real
mirrored ERP via mcp-minierp, calling tools one at a time through
/dashboard/try-tool -- the exact same governed path (_govern: PDP + redaction
+ audit) the Playground page uses, as a real staff user would.

Confirmed with the user before running: db-api.frontierdental.com is a mirror
of production; read-only lookups are fine to test with.

Steps:
  1. get_customers_by_region -- find real customers (tries a few candidate
     territories since we don't know the real book of business ahead of time).
  2. get_customer_order_summary per customer -- real lastOrderDate/orderCount/
     grandTotal from the live ERP.
  3. Compute "days since last order" ourselves and rank most-overdue first --
     no node in the graph model can do this today (see the "My Workflow"
     prompt-input gap discussed with the user: llm_transform can't take a
     table in and produce a ranked table out).
  4. create_pdf_packet with the REAL ranked list, and create_email_draft (never
     sent) for the single most-overdue customer -- proves the full chain: real
     ERP read -> business-rule ranking -> governed artifacts.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "winback-radar-live"
OUT = ROOT / "_smoke" / ".tmp" / "winback-radar-live-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "winback_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "winback_password",
    "GOVERNANCE_SESSION_SECRET": "winback-radar-live-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "MINIERP_MCP_URL": "http://127.0.0.1:18421/mcp",
    "OFFICE_MCP_URL": "http://127.0.0.1:18422/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18423/mcp",
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


def days_since(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(date_str[:26], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    now = datetime.now(timezone.utc) if dt.tzinfo is not None else datetime.now()
    return (now - dt).days


print("start mcp-minierp (real ERP mirror) + mcp-office + mcp-email")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
minierp = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18421", "--no-access-log"],
    cwd=str(ROOT / "mcp-minierp"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18422", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18423", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18421/health")
    wait_health("http://127.0.0.1:18422/health")
    wait_health("http://127.0.0.1:18423/health")
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
        consumer_id="user:winback_tester", name="winback_tester", key_hash="", status="active",
        role="user", type="user", categories=["accounts", "orders", "office", "email_draft"],
        login_password_hash=hash_password("tester_password"),
    ))

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "winback_tester", "password": "tester_password"})

        def try_tool(tool, args, customer_id=""):
            resp = client.post("/dashboard/try-tool", json={"tool": tool, "args": args, "customer_id": customer_id})
            return resp

        # ── Step 1: find real customers -- try a few plausible territories ──
        customers = []
        tried = []
        for kwargs in (
            {"country": "US"}, {"country": "USA"}, {"state": "CA"}, {"country": "US", "state": "CA"},
            {"country": "CA"}, {"state": "TX"}, {"city": "a"},
        ):
            resp = try_tool("get_customers_by_region", {**kwargs, "page": 1, "page_size": 25})
            tried.append((kwargs, resp.status_code))
            if resp.status_code != 200:
                continue
            result = (resp.json() or {}).get("result") or {}
            found = result.get("customers") or []
            if found:
                customers = found
                check(f"found real customers via get_customers_by_region({kwargs})", True)
                break
        check("at least one real customer found", len(customers) > 0, tried)

        if not customers:
            print("No customers found via any tried territory -- cannot continue the live test.")
            print("Tried:", tried)
        else:
            print(f"  -> {len(customers)} real customers returned, checking order history for up to 15 of them")

            # ── Step 2: real order-history summary per customer ────────────
            radar = []
            for c in customers[:15]:
                cid = c.get("customerId")
                if not cid:
                    continue
                resp = try_tool("get_customer_order_summary", {"start_date": "", "end_date": ""}, customer_id=cid)
                if resp.status_code != 200:
                    continue
                result = (resp.json() or {}).get("result") or {}
                if result.get("status") != "ok" or not result.get("orderCount"):
                    continue  # no order history to compute cadence from
                last = result.get("lastOrderDate")
                gap = days_since(last)
                radar.append({
                    "customerId": cid, "name": c.get("name") or cid,
                    "lastOrderDate": last, "daysSinceLastOrder": gap,
                    "orderCount": result.get("orderCount"), "grandTotal": result.get("grandTotal"),
                    "averageOrderValue": result.get("averageOrderValue"),
                })
            check("computed real order-history summaries", len(radar) > 0, radar)

            if radar:
                # ── Step 3: the actual business rule -- rank most-overdue first ─
                radar_ranked = sorted([r for r in radar if r["daysSinceLastOrder"] is not None],
                                       key=lambda r: r["daysSinceLastOrder"], reverse=True)
                check("ranked customers by real days-since-last-order", len(radar_ranked) > 0, radar_ranked)
                top = radar_ranked[:10]
                for r in top[:5]:
                    print(f"    {r['name']} ({r['customerId']}): {r['daysSinceLastOrder']} days since last order, "
                          f"{r['orderCount']} orders, avg {r['averageOrderValue']}")

                # ── Step 4: governed PDF with the REAL ranked list ──────────
                sections = [{"heading": "Reorder-Due / Win-Back Radar", "bullets": [
                    f"Customers checked: {len(radar)}",
                    f"Flagged as reorder-overdue (ranked by days since last order): {len(top)}",
                ]}]
                tables = [{"name": "Win-Back Radar", "rows": [
                    {"Customer": r["name"], "Customer ID": r["customerId"], "Days Since Last Order": r["daysSinceLastOrder"],
                     "Last Order Date": r["lastOrderDate"], "Order Count": r["orderCount"], "Avg Order Value": r["averageOrderValue"]}
                    for r in top
                ]}]
                pdf_resp = try_tool("create_pdf_packet", {
                    "title": "Reorder-Due / Win-Back Radar", "sections": sections, "tables": tables,
                    "classification": ["INTERNAL"],
                })
                check("create_pdf_packet call succeeds", pdf_resp.status_code == 200, pdf_resp.text)
                pdf_result = (pdf_resp.json() or {}).get("result") or {}
                aid = pdf_result.get("artifactId")
                check("PDF artifact created", bool(aid), pdf_result)

                if aid:
                    dl = client.get(f"/artifacts/{aid}/download")
                    check("PDF artifact downloads", dl.status_code == 200, dl.status_code)
                    (OUT / "winback-radar-real-data.pdf").write_bytes(dl.content)
                    text = pdf_text(dl.content)
                    check("PDF contains the real top-ranked customer's name", top[0]["name"] in text, text[:600])
                    check("PDF contains a real customer id from the radar", top[0]["customerId"] in text, text[:600])
                    check("PDF is not just a title (regression check)", len(text.splitlines()) > 5, text)

                # ── Bonus: draft (never send) a win-back email for the #1 account ─
                lead = top[0]
                draft_resp = try_tool("create_email_draft", {
                    "to": ["account-manager@frontierdental.com"],
                    "subject": f"Win-back follow-up: {lead['name']}",
                    "body_markdown": (
                        f"Hi,\n\n{lead['name']} ({lead['customerId']}) hasn't reordered in "
                        f"{lead['daysSinceLastOrder']} days (avg order value {lead['averageOrderValue']}). "
                        "Worth a quick check-in call before they move to another supplier.\n\n"
                        "Regards,\nGoverned AI Office Assistant"
                    ),
                    "classification": ["INTERNAL"],
                })
                check("win-back email drafted for the top-ranked real customer", draft_resp.status_code == 200, draft_resp.text)
                draft_result = (draft_resp.json() or {}).get("result") or {}
                check("draft was created, not sent (draftId present, no sendId)",
                      bool(draft_result.get("draftId")) and not draft_result.get("sendId"), draft_result)

                (OUT / "winback-radar-ranked-data.json").write_text(json.dumps(top, indent=2), encoding="utf-8")

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
