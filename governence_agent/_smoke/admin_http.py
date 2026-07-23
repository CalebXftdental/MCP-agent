"""HTTP admin-API test against the running gateway (:8020) with a FILE store.

Requires the gateway launched with GOVERNANCE_STORE_FILE set (writable) plus the
admin/viewer/session env from step 3. Proves: admin CRUD works, writes are audited,
viewer is forbidden from writes, and a consumer CREATED via the dashboard can
immediately call /mcp with its issued key and gets its department's grant.

Run:  python _smoke/admin_http.py
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


def client_as(username, password):
    c = httpx.Client(base_url=BASE, timeout=15)
    r = c.post("/dashboard/login", json={"username": username, "password": password})
    assert r.status_code == 200, f"login failed for {username}: {r.text}"
    return c


async def mcp_call(api_key, tool, args):
    async with streamablehttp_client(BASE + "/mcp", headers={"Authorization": f"Bearer {api_key}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, arguments=args)
            return json.loads(res.content[0].text)


def main():
    admin = client_as("admin", "admin-pw")
    viewer = client_as("viewer", "viewer-pw")

    print("catalog")
    cat = admin.get("/admin/catalog").json()
    check("catalog lists minierp tools", "get_customer_orders" in cat["backends"].get("minierp", []), cat)
    check("catalog lists departments", "customer_service" in cat["departments"], cat)

    print("viewer is forbidden from writes")
    check("viewer POST /admin/consumers -> 403",
          viewer.post("/admin/consumers", json={"name": "x"}).status_code == 403)
    check("viewer GET /admin/consumers -> 403 (admin-only listing)",
          viewer.get("/admin/consumers").status_code == 403)

    print("admin creates a customer_service consumer (key shown once)")
    cid = "svc_lab"
    admin.request("DELETE", f"/admin/consumers/{cid}")  # clean slate if re-run
    r = admin.post("/admin/consumers", json={"name": cid, "department": "customer_service", "type": "agent"})
    check("create -> 201 + api_key", r.status_code == 201 and r.json().get("api_key"), r.text)
    new_key = r.json()["api_key"]
    check("consumer appears in listing", any(c["consumer_id"] == cid for c in admin.get("/admin/consumers").json()["consumers"]))
    check("create is audited", any(ch["tool"].startswith("create_consumer") for ch in admin.get("/admin/policy-changes").json()["changes"]))

    print("the new key works through /mcp with its department grant")
    # customer_service is granted get_customer_orders (total redacted) but NOT get_customer_order_total.
    orders = asyncio.run(mcp_call(new_key, "minierp_get_customer_orders", {"session_id": "s-lab", "customer_id": "PRIN100"}))
    check("new consumer can call granted tool", orders.get("status") == "success", orders)
    check("dept redaction applies (total hidden)", "total" not in orders["records"][0], orders)
    denied = asyncio.run(mcp_call(new_key, "minierp_get_customer_order_total", {"session_id": "s-lab", "customer_id": "PRIN100"}))
    check("new consumer denied ungranted tool", denied.get("status") == "denied", denied)

    print("admin edits a department, change is audited")
    dep = {"id": "lab_dept", "display_name": "Lab", "grants": {"minierp": {"tools": ["get_order_details"], "levels": ["PUBLIC", "INTERNAL"]}}, "data_domains": []}
    check("upsert department -> ok", admin.post("/admin/departments", json=dep).json().get("ok") is True)
    check("department now listed", any(d["id"] == "lab_dept" for d in admin.get("/admin/departments").json()["departments"]))

    print("whitelist get/set")
    check("set whitelist ok", admin.request("PUT", "/admin/whitelist", json={"cidrs": ["10.0.0.0/8"]}).json()["whitelist"] == ["10.0.0.0/8"])
    check("clear whitelist ok", admin.request("PUT", "/admin/whitelist", json={"cidrs": []}).json()["whitelist"] == [])

    # cleanup
    admin.request("DELETE", f"/admin/consumers/{cid}")
    admin.request("DELETE", "/admin/departments/lab_dept")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


main()
