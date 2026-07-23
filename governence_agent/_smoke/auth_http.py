"""HTTP auth/RBAC test against the running gateway (:8020).

Assumes the gateway is up with:
  GOVERNANCE_SESSION_SECRET set, GOVERNANCE_COOKIE_SECURE=false (local http),
  GOVERNANCE_ADMIN_USER=admin  GOVERNANCE_ADMIN_PASSWORD=admin-pw
  GOVERNANCE_VIEWER_USER=viewer GOVERNANCE_VIEWER_PASSWORD=viewer-pw
  GOVERNANCE_LOGIN_MAX_FAILURES=3
  a consumer API key GOVERNANCE_KEY_CHATBOT=dev-chatbot-key

Run:  python _smoke/auth_http.py
"""
import sys

import httpx

BASE = "http://localhost:8020"
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {name}")
    else:
        FAIL += 1; print(f"  FAIL {name} {extra}")


def login(username, password):
    c = httpx.Client(base_url=BASE, timeout=10)
    r = c.post("/dashboard/login", json={"username": username, "password": password})
    return c, r


print("no session -> protected routes rejected")
anon = httpx.Client(base_url=BASE, timeout=10)
check("/admin/calls 401 without cookie", anon.get("/admin/calls").status_code == 401)
check("/dashboard 401 (login page) without cookie", anon.get("/dashboard").status_code == 401)
check("/dashboard/admin 401 without cookie", anon.get("/dashboard/admin").status_code == 401)

print("bad credentials + lockout")
# Target a throwaway username so the lockout doesn't lock the real admin
# (lockout key is username|ip, and everything here shares the localhost IP).
r1 = anon.post("/dashboard/login", json={"username": "attacker", "password": "wrong"})
check("bad login -> 401", r1.status_code == 401)
anon.post("/dashboard/login", json={"username": "attacker", "password": "wrong"})
anon.post("/dashboard/login", json={"username": "attacker", "password": "wrong"})
r_locked = anon.post("/dashboard/login", json={"username": "attacker", "password": "wrong"})
check("locked out after 3 failures -> 429", r_locked.status_code == 429, r_locked.status_code)

print("admin login + RBAC (fresh IP-less client)")
ac, ar = login("admin", "admin-pw")
check("admin login 200 + cookie", ar.status_code == 200 and "gov_session" in ac.cookies, ar.text)
check("admin /me shows admin role", ac.get("/dashboard/me").json().get("role") == "admin")
check("admin sees /admin/calls", ac.get("/admin/calls").status_code == 200)
check("admin sees /admin/sessions", ac.get("/admin/sessions").status_code == 200)
check("admin sees /admin/rate-limits", ac.get("/admin/rate-limits").status_code == 200)
check("admin /dashboard/admin 200", ac.get("/dashboard/admin").status_code == 200)

print("viewer login + RBAC (restricted)")
vc, vr = login("viewer", "viewer-pw")
check("viewer login 200", vr.status_code == 200)
check("viewer /me shows user role", vc.get("/dashboard/me").json().get("role") == "user")
check("viewer can read /admin/calls (scoped)", vc.get("/admin/calls").status_code == 200)
check("viewer FORBIDDEN /admin/sessions (403)", vc.get("/admin/sessions").status_code == 403)
check("viewer FORBIDDEN /dashboard/admin (403)", vc.get("/dashboard/admin").status_code == 403)

print("logout clears session")
ac.post("/dashboard/logout")
check("after logout /admin/sessions 401", ac.get("/admin/sessions").status_code == 401)

print("MCP endpoint still requires API key (not a session)")
check("/mcp without key 401", anon.post("/mcp", json={}).status_code in (401, 400))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
