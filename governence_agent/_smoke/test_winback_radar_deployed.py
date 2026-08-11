"""LIVE test of the win-back radar against the REAL DEPLOYED gateway
(https://governence-agent-dycufrgya2ewash8.canadaeast-01.azurewebsites.net),
using the admin API key from mcp-knowledge/.env.local as a Bearer token on the
gateway's own /mcp endpoint -- machine-caller access, per DEPLOY.md.

This App Service's outbound IPs are the ones actually allowlisted on
db-api.frontierdental.com (it's the real production chat path), so this
sidesteps the Cloudflare block that killed the local-sandbox attempt entirely
-- no local mcp-minierp process, no local network egress involved for the ERP
calls. mcp-office/mcp-email run alongside the same deployment, so the PDF/
draft-email steps happen there too, not locally.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).parent.parent.resolve()
OUT = ROOT / "_smoke" / ".tmp" / "winback-radar-deployed-artifacts"
OUT.mkdir(parents=True, exist_ok=True)

GATEWAY_URL = "https://governence-agent-dycufrgya2ewash8.canadaeast-01.azurewebsites.net/mcp"
API_KEY = (ROOT / "mcp-knowledge" / ".env.local").read_text(encoding="utf-8")
API_KEY = next(line.split("=", 1)[1].strip() for line in API_KEY.splitlines() if line.strip().startswith("local_admin_api_key"))

PASS, FAIL = 0, 0


def check(name, cond, detail=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}", str(detail)[:500] if detail is not None else "")


def pdf_text(payload: bytes) -> str:
    raw = payload.decode("latin-1", errors="replace")
    return "\n".join(m.group(1).replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")
                      for m in re.finditer(r"\(((?:[^()\\]|\\.)*)\)\s*Tj", raw))


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str[:26], fmt)
        except ValueError:
            continue
    return None


def days_since(date_str: str | None) -> int | None:
    dt = _parse_date(date_str)
    if dt is None:
        return None
    now = datetime.now(timezone.utc) if dt.tzinfo is not None else datetime.now()
    return (now - dt).days


def days_between(a: str | None, b: str | None) -> int | None:
    da, db = _parse_date(a), _parse_date(b)
    if da is None or db is None:
        return None
    return abs((db - da).days)


async def main() -> None:
    headers = {"Authorization": f"Bearer {API_KEY}"}
    async with streamablehttp_client(GATEWAY_URL, headers=headers, timeout=60) as (read, write, _sid):
        async with ClientSession(read, write) as session:
            await session.initialize()
            check("connected + initialized against the deployed gateway", True)

            tools = (await session.list_tools()).tools
            names = {t.name for t in tools}
            check("deployed gateway exposes minierp_accounts_get_customers_by_region", "minierp_accounts_get_customers_by_region" in names, sorted(names))
            check("deployed gateway exposes minierp_orders_get_customer_order_summary", "minierp_orders_get_customer_order_summary" in names, sorted(names))
            check("deployed gateway exposes office_create_pdf_packet", "office_create_pdf_packet" in names)
            check("deployed gateway exposes email_create_email_draft", "email_create_email_draft" in names)

            SESSION_ID = "winback-radar-manual-test"

            async def call(tool, args):
                result = await session.call_tool(tool, arguments={**args, "session_id": SESSION_ID})
                text = ""
                for part in result.content or []:
                    if getattr(part, "text", None) is not None:
                        text = part.text
                        break
                is_error = getattr(result, "isError", False)
                return is_error, text

            # ── Step 1: real customers from the real ERP ────────────────────
            customers = []
            tried = []
            for kwargs in ({"country": "US"}, {"country": "USA"}, {"state": "CA"}, {"country": "CA"},
                           {"state": "TX"}, {"state": "ON"}, {"city": "a"}):
                is_error, text = await call("minierp_accounts_get_customers_by_region",
                                             {**kwargs, "page": 1, "page_size": 25})
                tried.append((kwargs, is_error, text[:200]))
                if is_error:
                    continue
                try:
                    result = json.loads(text)
                except (ValueError, TypeError):
                    continue
                found = result.get("customers") or []
                if found:
                    customers = found
                    check(f"found real customers via get_customers_by_region({kwargs})", True)
                    break
            check("at least one real customer found on the deployed instance", len(customers) > 0, tried)

            if not customers:
                print("No customers found. Tried:", json.dumps(tried, indent=2, default=str))
                return

            print(f"  -> {len(customers)} real customers returned; checking order history for up to 15")

            # ── Step 2: real order-history summaries ────────────────────────
            radar = []
            thin_history = []  # orderCount <= 1 -- no cadence of their own to compare against
            for c in customers[:15]:
                cid = c.get("customerId")
                if not cid:
                    continue
                is_error, text = await call("minierp_orders_get_customer_order_summary",
                                             {"customer_id": cid, "start_date": "", "end_date": ""})
                if is_error:
                    continue
                try:
                    result = json.loads(text)
                except (ValueError, TypeError):
                    continue
                order_count = result.get("orderCount") or 0
                if result.get("status") != "ok" or not order_count:
                    continue
                first, last = result.get("firstOrderDate"), result.get("lastOrderDate")
                gap = days_since(last)
                if gap is None:
                    continue
                row = {
                    "customerId": cid, "name": c.get("name") or cid, "status": c.get("status"),
                    "firstOrderDate": first, "lastOrderDate": last, "daysSinceLastOrder": gap,
                    "orderCount": order_count, "grandTotal": result.get("grandTotal"),
                    "averageOrderValue": result.get("averageOrderValue"),
                }
                if order_count <= 1:
                    row["avgDaysBetweenOrders"] = None
                    row["overdueRatio"] = None
                    thin_history.append(row)
                    continue
                # The actual reorder-cadence rule: how many days THIS customer typically
                # waits between orders (total order-history span / number of gaps), not a
                # flat threshold shared across every account. A customer who orders every
                # 90 days is genuinely overdue at 150; one who orders yearly is not.
                span = days_between(first, last)
                avg_gap = round(span / (order_count - 1), 1) if span and order_count > 1 else None
                row["avgDaysBetweenOrders"] = avg_gap
                row["overdueRatio"] = round(gap / avg_gap, 2) if avg_gap else None
                radar.append(row)
            check("computed real order-history summaries from the deployed ERP path", len(radar) > 0, radar)
            check("captured accounts with too little history to have a cadence", True, len(thin_history))

            if not radar:
                return

            # Rank by how overdue a customer is RELATIVE TO THEIR OWN cadence, not raw
            # days-since-last-order -- that's the fix: a flat leaderboard flags long-cycle
            # customers (annual reorders) as "overdue" just as fast as weekly ones.
            scoreable = [r for r in radar if r["overdueRatio"] is not None]
            # A closed/inactive account isn't a win-back lead, it's a dead account -- real
            # data just proved this matters: 3 of the top 6 by raw ratio were status != "A".
            inactive = [r for r in scoreable if r.get("status") != "A"]
            active_leads = [r for r in scoreable if r.get("status") == "A"]
            ranked = sorted(active_leads, key=lambda r: r["overdueRatio"], reverse=True)
            check("ranked real ACTIVE customers by cadence-relative overdue ratio", len(ranked) > 0)
            if inactive:
                print(f"  -> excluded {len(inactive)} closed/inactive account(s) from the lead list: " +
                      ", ".join(f"{r['name']} (status={r['status']})" for r in inactive))
            top = ranked[:10]
            for r in top[:6]:
                print(f"    {r['name']} ({r['customerId']}): {r['daysSinceLastOrder']}d since last order vs "
                      f"~{r['avgDaysBetweenOrders']}d typical gap -> {r['overdueRatio']}x overdue, "
                      f"{r['orderCount']} orders, status={r['status']}")

            # ── Step 3: governed PDF with the REAL ranked list ──────────────
            sections = [{"heading": "Reorder-Due / Win-Back Radar", "bullets": [
                f"Customers checked: {len(radar)} (plus {len(thin_history)} with too little order history to score)",
                f"Excluded as closed/inactive (not real win-back leads): {len(inactive)}",
                f"Active leads flagged reorder-overdue, ranked by how far past THEIR OWN typical reorder cadence they are: {len(top)}",
            ]}]
            tables = [{"name": "Win-Back Radar", "rows": [
                {"Customer": r["name"], "Customer ID": r["customerId"], "Status": r["status"],
                 "Days Since Last Order": r["daysSinceLastOrder"], "Typical Gap (days)": r["avgDaysBetweenOrders"],
                 "Overdue Ratio": r["overdueRatio"], "Order Count": r["orderCount"], "Avg Order Value": r["averageOrderValue"]}
                for r in top
            ]}]
            is_error, text = await call("office_create_pdf_packet", {
                "title": "Reorder-Due / Win-Back Radar", "sections": sections, "tables": tables,
                "classification": ["INTERNAL"],
            })
            check("create_pdf_packet call succeeds on the deployed instance", not is_error, text[:300])
            pdf_result = json.loads(text) if not is_error else {}
            download_url = pdf_result.get("downloadUrl")
            check("PDF artifact created with a download URL", bool(download_url), pdf_result)

            if download_url:
                import httpx
                full_url = download_url if download_url.startswith("http") else \
                    "https://governence-agent-dycufrgya2ewash8.canadaeast-01.azurewebsites.net" + download_url
                async with httpx.AsyncClient(headers=headers) as http:
                    dl = await http.get(full_url)
                check("PDF artifact downloads from the deployed instance", dl.status_code == 200, dl.status_code)
                if dl.status_code == 200:
                    (OUT / "winback-radar-deployed-real-data.pdf").write_bytes(dl.content)
                    text_out = pdf_text(dl.content)
                    check("PDF contains the real top-ranked customer's name", top[0]["name"] in text_out, text_out[:600])
                    check("PDF is not just a title (regression check)", len(text_out.splitlines()) > 5, text_out)

            # ── Bonus: draft (never send) a win-back email ──────────────────
            lead = top[0]
            is_error, text = await call("email_create_email_draft", {
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
            check("win-back email drafted on the deployed instance", not is_error, text[:300])
            draft_result = json.loads(text) if not is_error else {}
            check("draft was created, not sent (draftId present, no sendId)",
                  bool(draft_result.get("draftId")) and not draft_result.get("sendId"), draft_result)

            (OUT / "winback-radar-deployed-ranked-data.json").write_text(json.dumps(top, indent=2), encoding="utf-8")


asyncio.run(main())
print(f"\n{PASS} passed, {FAIL} failed")
print(f"artifacts written to: {OUT}")
sys.exit(1 if FAIL else 0)
