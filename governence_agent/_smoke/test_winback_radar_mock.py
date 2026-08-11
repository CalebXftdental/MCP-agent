"""MOCK-data fallback for the win-back radar test (see test_winback_radar_live.py).

The live ERP call is currently blocked by what looks like network/WAF-level
403s (identical failure across all credentials/regions -- an IE6-compat HTML
block page, not an Acumatica auth error), independent of this test's logic.
This proves the rest of the chain for real while that's sorted out:

  - The customer order-history data below is FABRICATED (clearly labeled),
    standing in for what get_customer_order_summary would return.
  - The ranking rule (days-since-last-order, most overdue first) is REAL code,
    the same business rule the live test applies to real data.
  - create_pdf_packet and create_email_draft are called through the REAL
    governed pipeline (/dashboard/try-tool -> _govern) against a REAL local
    mcp-office/mcp-email backend -- not mocked, not skipped.

Swap this file's MOCK_CUSTOMERS block for a real get_customers_by_region /
get_customer_order_summary pull once ERP network access is confirmed, and
everything downstream (ranking, PDF, draft) is unchanged.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "winback-radar-mock"
OUT = ROOT / "_smoke" / ".tmp" / "winback-radar-mock-artifacts"
shutil.rmtree(TMP, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

os.environ.update({
    "GOVERNANCE_ADMIN_USER": "winback_admin",
    "GOVERNANCE_ADMIN_PASSWORD": "winback_password",
    "GOVERNANCE_SESSION_SECRET": "winback-radar-mock-secret",
    "GOVERNANCE_COOKIE_SECURE": "false",
    "GOVERNANCE_ARTIFACT_DIR": str(TMP / "artifacts"),
    "GOVERNANCE_STATE_DIR": str(TMP / "state"),
    "GOVERNANCE_STORE_FILE": str(TMP / "policy-store.json"),
    "OFFICE_MCP_URL": "http://127.0.0.1:18432/mcp",
    "EMAIL_MCP_URL": "http://127.0.0.1:18433/mcp",
    "GATEWAY_BACKEND_TIMEOUT_SEC": "15",
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


def wait_health(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"service did not become healthy: {last}")


def pdf_text(payload: bytes) -> str:
    raw = payload.decode("latin-1", errors="replace")
    return "\n".join(m.group(1).replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
                      for m in re.finditer(r"\(((?:[^()\\]|\\.)*)\)\s*Tj", raw))


def days_since(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return (datetime.now() - dt).days


# ── FABRICATED order-history data, shaped exactly like get_customer_order_summary's
#    real output (customerId, name, lastOrderDate, orderCount, grandTotal,
#    averageOrderValue) -- standing in for the live ERP pull. ──────────────────
MOCK_CUSTOMERS = [
    {"customerId": "CUST-100", "name": "Sample Dental Group", "lastOrderDate": "2026-05-02", "orderCount": 14, "grandTotal": 58200.00, "averageOrderValue": 4157.14},
    {"customerId": "CUST-245", "name": "North Clinic Network", "lastOrderDate": "2026-07-28", "orderCount": 22, "grandTotal": 91300.00, "averageOrderValue": 4150.00},
    {"customerId": "CUST-311", "name": "Prairie Ortho", "lastOrderDate": "2026-06-10", "orderCount": 9, "grandTotal": 24750.50, "averageOrderValue": 2750.06},
    {"customerId": "CUST-420", "name": "Harbour Hygiene", "lastOrderDate": "2026-08-01", "orderCount": 7, "grandTotal": 19880.25, "averageOrderValue": 2840.04},
    {"customerId": "CUST-511", "name": "Capital Endodontics", "lastOrderDate": "2026-04-15", "orderCount": 5, "grandTotal": 17440.10, "averageOrderValue": 3488.02},
    {"customerId": "CUST-618", "name": "Lakeside Family Dental", "lastOrderDate": "2026-07-15", "orderCount": 11, "grandTotal": 33020.00, "averageOrderValue": 3002.00},
]

print("start mcp-office + mcp-email")
python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
office = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18432", "--no-access-log"],
    cwd=str(ROOT / "mcp-office"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
email = subprocess.Popen(
    [str(python_exe), "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "18433", "--no-access-log"],
    cwd=str(ROOT / "mcp-email"), env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
try:
    wait_health("http://127.0.0.1:18432/health")
    wait_health("http://127.0.0.1:18433/health")
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
        role="user", type="user", categories=["office", "email_draft"],
        login_password_hash=hash_password("tester_password"),
    ))

    with TestClient(gateway_app.app, base_url="http://testserver") as client:
        client.post("/dashboard/login", json={"username": "winback_tester", "password": "tester_password"})

        def try_tool(tool, args, customer_id=""):
            return client.post("/dashboard/try-tool", json={"tool": tool, "args": args, "customer_id": customer_id})

        # ── The real business rule, applied to the fabricated order history ──
        radar = [{**c, "daysSinceLastOrder": days_since(c["lastOrderDate"])} for c in MOCK_CUSTOMERS]
        ranked = sorted(radar, key=lambda r: r["daysSinceLastOrder"], reverse=True)
        check("ranked mock customers by days-since-last-order", len(ranked) == len(MOCK_CUSTOMERS))
        check("most-overdue customer is ranked first",
              ranked[0]["customerId"] == max(radar, key=lambda r: r["daysSinceLastOrder"])["customerId"], ranked)
        for r in ranked:
            print(f"    {r['name']} ({r['customerId']}): {r['daysSinceLastOrder']} days since last order, "
                  f"{r['orderCount']} orders, avg {r['averageOrderValue']}")

        # ── Governed PDF (real call, real local mcp-office) ─────────────────
        sections = [{"heading": "Reorder-Due / Win-Back Radar (MOCK DATA -- real ERP access pending)", "bullets": [
            f"Customers checked: {len(radar)}",
            f"Ranked most reorder-overdue first",
        ]}]
        tables = [{"name": "Win-Back Radar", "rows": [
            {"Customer": r["name"], "Customer ID": r["customerId"], "Days Since Last Order": r["daysSinceLastOrder"],
             "Last Order Date": r["lastOrderDate"], "Order Count": r["orderCount"], "Avg Order Value": r["averageOrderValue"]}
            for r in ranked
        ]}]
        pdf_resp = try_tool("create_pdf_packet", {
            "title": "Reorder-Due / Win-Back Radar (mock data)", "sections": sections, "tables": tables,
            "classification": ["INTERNAL"],
        })
        check("create_pdf_packet call succeeds", pdf_resp.status_code == 200, pdf_resp.text)
        pdf_result = (pdf_resp.json() or {}).get("result") or {}
        aid = pdf_result.get("artifactId")
        check("PDF artifact created", bool(aid), pdf_result)

        if aid:
            dl = client.get(f"/artifacts/{aid}/download")
            check("PDF artifact downloads", dl.status_code == 200, dl.status_code)
            (OUT / "winback-radar-mock.pdf").write_bytes(dl.content)
            text = pdf_text(dl.content)
            check("PDF contains the most-overdue customer's name", ranked[0]["name"] in text, text[:600])
            check("PDF contains every mock customer's name", all(c["name"] in text for c in MOCK_CUSTOMERS), text)
            check("PDF is not just a title (regression check)", len(text.splitlines()) > 5, text)

        # ── Draft (never send) a win-back email for the most-overdue account ─
        lead = ranked[0]
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
        check("win-back email drafted for the most-overdue customer", draft_resp.status_code == 200, draft_resp.text)
        draft_result = (draft_resp.json() or {}).get("result") or {}
        draft_id = draft_result.get("draftId")
        check("draft was created, not sent (draftId present, no sendId)",
              bool(draft_id) and not draft_result.get("sendId"), draft_result)

        if draft_id:
            draft_dl = client.get(f"/artifacts/{draft_id}/download")
            check("draft artifact downloads", draft_dl.status_code == 200, draft_dl.status_code)
            draft_body = json.loads(draft_dl.content.decode("utf-8"))
            check("draft body actually mentions the specific customer",
                  lead["name"] in (draft_body.get("body_markdown") or ""), draft_body)
            check("draft body mentions the real day count",
                  str(lead["daysSinceLastOrder"]) in (draft_body.get("body_markdown") or ""), draft_body)

        (OUT / "winback-radar-mock-ranked-data.json").write_text(json.dumps(ranked, indent=2), encoding="utf-8")

finally:
    office.terminate()
    email.terminate()
    for proc in (office, email):
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

print(f"\n{PASS} passed, {FAIL} failed")
print(f"artifacts written to: {OUT}")
sys.exit(1 if FAIL else 0)
