"""HTTP lifecycle test: signup -> pending -> approve -> self-mint key -> use it,
then access-request -> approve -> widened grant, plus denial-derived suggestions.

Requires the gateway up with a FILE store (GOVERNANCE_STORE_FILE) + admin creds +
session secret + non-secure cookie (see step-4 launch).

Run:  python _smoke/lifecycle_http.py
"""
import asyncio
import json
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = "http://localhost:8020"
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {name}")
    else:
        FAIL += 1; print(f"  FAIL {name} {extra}")


def client():
    return httpx.Client(base_url=BASE, timeout=15)


def login(username, password):
    c = client()
    r = c.post("/dashboard/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login {username}: {r.text}"
    return c


async def mcp_call(api_key, tool, args):
    async with streamablehttp_client(BASE + "/mcp", headers={"Authorization": f"Bearer {api_key}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, arguments=args)
            return json.loads(res.content[0].text)


def main():
    admin = login("admin", "admin-pw")
    uname = "alice_cs"
    # clean slate if re-run
    admin.request("DELETE", f"/admin/consumers/user:{uname}")

    print("self-service signup -> pending")
    anon = client()
    r = anon.post("/dashboard/signup", json={"username": uname, "password": "pw12345",
                                             "department": "customer_service", "use_case": "handle order status"})
    check("signup -> 201 pending", r.status_code == 201 and r.json().get("status") == "pending", r.text)

    print("pending user can log in but has no access / no key")
    user = login(uname, "pw12345")
    acc = user.get("/dashboard/my-access").json()
    check("my-access shows pending", acc.get("status") == "pending", acc)
    check("pending has no access", acc.get("access") == {}, acc)
    check("pending cannot mint key (403)", user.post("/dashboard/my-key/rotate").status_code == 403)

    print("admin sees the pending account request + approves it")
    reqs = admin.get("/admin/requests?status=pending").json()["requests"]
    acct_req = next((r for r in reqs if r["kind"] == "account" and r["username"] == uname), None)
    check("account request present", acct_req is not None, reqs)
    check("approve -> ok", admin.post(f"/admin/requests/{acct_req['id']}/approve", json={}).json().get("ok") is True)

    print("approved user self-mints a key and it works through /mcp")
    macc = user.get("/dashboard/my-access").json()
    check("now active", macc.get("status") == "active", macc)
    check("has customer_service access", "get_customer_orders" in macc["access"].get("minierp", {}).get("tools", []), macc)
    key = user.post("/dashboard/my-key/rotate").json()["api_key"]
    orders = asyncio.run(mcp_call(key, "minierp_get_customer_orders", {"session_id": "s-al", "customer_id": "PRIN100"}))
    check("minted key works on granted tool", orders.get("status") == "success", orders)
    check("dept redaction (total hidden)", "total" not in orders["records"][0], orders)

    print("ungranted tool denied -> shows up as an access suggestion")
    denied = asyncio.run(mcp_call(key, "minierp_get_customer_order_total", {"session_id": "s-al", "customer_id": "PRIN100"}))
    check("ungranted tool denied", denied.get("status") == "denied", denied)
    sugg = admin.get("/admin/access-suggestions").json()["suggestions"]
    check("denial surfaced as suggestion", any(s["consumer"] == uname and "order_total" in s["tool"] for s in sugg), sugg)

    print("user requests the extra tool -> admin approves -> grant widens")
    rid = user.post("/dashboard/request-access", json={"backend": "minierp",
          "tools": ["get_customer_order_total"], "levels": ["SENSITIVE"],
          "justification": "need spend totals"}).json()["id"]
    check("approve access request", admin.post(f"/admin/requests/{rid}/approve", json={}).json().get("ok") is True)
    total = asyncio.run(mcp_call(key, "minierp_get_customer_order_total", {"session_id": "s-al2", "customer_id": "PRIN100"}))
    check("previously-denied tool now works", total.get("status") == "success", total)
    check("granted SENSITIVE -> grandTotal visible", total.get("grandTotal") is not None, total)

    print("deny path: signup a second user and deny it")
    admin.request("DELETE", "/admin/consumers/user:tempbob")
    client().post("/dashboard/signup", json={"username": "tempbob", "password": "pw12345", "use_case": "x"})
    reqs2 = admin.get("/admin/requests?status=pending").json()["requests"]
    bob_req = next(r for r in reqs2 if r.get("username") == "tempbob")
    check("deny -> ok", admin.post(f"/admin/requests/{bob_req['id']}/deny", json={}).json().get("ok") is True)
    check("denied signup cannot log in",
          client().post("/dashboard/login", json={"username": "tempbob", "password": "pw12345"}).status_code == 401)

    # cleanup
    admin.request("DELETE", f"/admin/consumers/user:{uname}")
    admin.request("DELETE", "/admin/consumers/user:tempbob")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


main()
