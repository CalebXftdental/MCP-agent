"""End-to-end client: calls the running Governance Gateway as an MCP client,
authenticating with different consumer keys, and asserts governance behavior.

Assumes:
  - mock_minierp.py running on :8021
  - gateway/app.py running on :8020 with the dev consumer keys from
    .env.local.example (chatbot/email_bot/analytics)

Run:  python _smoke/e2e_client.py
"""
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

GATEWAY = "http://localhost:8020/mcp"
KEYS = {"chatbot": "dev-chatbot-key", "email_bot": "dev-email-key",
        "analytics": "dev-analytics-key", "cs_bot": "dev-cs-key"}  # cs_bot -> customer_service dept
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {name}")
    else:
        FAIL += 1; print(f"  FAIL {name} {extra}")


async def call(consumer, tool, args):
    headers = {"Authorization": f"Bearer {KEYS[consumer]}"}
    async with streamablehttp_client(GATEWAY, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, arguments=args)
            return json.loads(res.content[0].text)


async def list_tools(consumer):
    headers = {"Authorization": f"Bearer {KEYS[consumer]}"}
    async with streamablehttp_client(GATEWAY, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return [t.name for t in (await s.list_tools()).tools]


async def main():
    tools = await list_tools("chatbot")
    print("federation")
    check("gateway advertises namespaced tools", "minierp_get_customer_orders" in tools, tools)
    check("8 minierp tools federated", len([t for t in tools if t.startswith("minierp_")]) == 8, tools)

    print("deny: account-scoped call without customer scope")
    r = await call("chatbot", "minierp_get_contacts", {"session_id": "s-deny"})
    check("denied w/ missing_identifier", r.get("status") == "missing_identifier", r)
    check("asks for customerId", r.get("missingFields") == ["customerId"], r)

    print("allow + per-consumer censorship: contacts")
    cb = await call("chatbot", "minierp_get_contacts", {"session_id": "s-cb", "customer_id": "PRIN100"})
    check("chatbot sees email", cb["records"][0].get("email") == "jane@frontier.com", cb)
    an = await call("analytics", "minierp_get_contacts", {"session_id": "s-an", "customer_id": "PRIN100"})
    check("analytics email masked", an["records"][0].get("email") == "j***@frontier.com", an)
    check("analytics phone masked", an["records"][0].get("phone") == "5***", an)

    print("session scope memory: customer_id remembered across calls")
    await call("chatbot", "minierp_get_contacts", {"session_id": "s-mem", "customer_id": "PRIN100"})
    r2 = await call("chatbot", "minierp_get_customer_orders", {"session_id": "s-mem"})  # no customer_id
    check("2nd call reuses session scope", r2.get("status") == "success", r2)
    check("chatbot sees order total", r2["records"][0].get("total") == 1999.50, r2)

    print("per-consumer censorship: order total (SENSITIVE)")
    eb = await call("email_bot", "minierp_get_customer_orders", {"session_id": "s-eb", "customer_id": "PRIN100"})
    check("email_bot total dropped", "total" not in eb["records"][0], eb)

    print("non-scoped tool works without scope")
    od = await call("email_bot", "minierp_get_order_details", {"session_id": "s-od", "order_number": "SO123"})
    check("order_details allowed", od.get("status") == "success", od)
    check("email_bot order total dropped", "total" not in od, od)

    print("department scoping (cs_bot = customer_service): tool-level deny + redaction")
    # customer_service is NOT granted the spend/total tool -> deny-by-default
    dt = await call("cs_bot", "minierp_get_customer_order_total", {"session_id": "s-cs", "customer_id": "PRIN100"})
    check("cs denied order_total (not_granted)", dt.get("status") == "denied", dt)
    # customer_service IS granted orders, but has no SENSITIVE level -> total redacted
    co = await call("cs_bot", "minierp_get_customer_orders", {"session_id": "s-cs", "customer_id": "PRIN100"})
    check("cs allowed customer_orders", co.get("status") == "success", co)
    check("cs order total redacted", "total" not in co["records"][0], co)
    # customer_service HAS PII -> contacts email visible
    cc = await call("cs_bot", "minierp_get_contacts", {"session_id": "s-cs", "customer_id": "PRIN100"})
    check("cs sees contact email (has PII)", cc["records"][0].get("email") == "jane@frontier.com", cc)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
