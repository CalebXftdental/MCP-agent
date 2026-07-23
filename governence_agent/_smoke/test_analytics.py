"""Offline security-analytics tests -- no servers, no network.

Run:  python _smoke/test_analytics.py
Seeds the audit ring buffer directly and asserts gateway/analytics.py: overview
KPIs/series/heatmap, incident grouping + severity, DURABLE (audit-derived) alert
status, traffic-inferred backend health (no live probe), and the per-consumer
risk profile's least-privilege (unused-grant) detection.
"""
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "governance_core"))
sys.path.insert(0, str(_ROOT / "gateway"))

# Use a temp *writable* file store so the risk-profile / least-privilege checks
# actually run (the default local store is read-only). Set before any get_store().
_STORE_FILE = Path(__file__).parent / f".analytics_smoke_store_{os.getpid()}.json"
os.environ["GOVERNANCE_STORE_FILE"] = str(_STORE_FILE)

import audit  # noqa: E402
import analytics  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


# Force the in-memory ring as the only sink so the test is hermetic regardless of
# any GOVERNANCE_AUDIT_FILE / Cosmos env the dev happens to have set.
audit._AUDIT_FILE = None
audit._cosmos_container = None
audit._cosmos_init_attempted = True

NOW = time.time()
ORDERS = "minierp_orders_get_customer_orders"   # has a SENSITIVE field (total)
INVOICE = "minierp_finance_get_invoice"


def seed():
    audit._records.clear()
    # 6 authorized reads by ara-bot, 6 distinct customers within 5 min -> enumeration
    for i in range(6):
        audit._records.append({"ts": NOW - 60 * i, "type": "call", "consumer": "ara-bot",
            "tool": ORDERS, "status": "ok", "latency_ms": 100, "customer_id": f"C-{i}",
            "rows": 10, "detail": "redacted: total", "client_ip": "10.0.0.1"})
    # 2 errors by ara-bot (older than the oks, so last_ok stays most recent -> healthy)
    for i in range(2):
        audit._records.append({"ts": NOW - 600 - i, "type": "call", "consumer": "ara-bot",
            "tool": ORDERS, "status": "error", "latency_ms": 100, "detail": "upstream", "client_ip": "10.0.0.1"})
    # 4 denials by bad-bot within 15 min -> denials incident
    for i in range(4):
        audit._records.append({"ts": NOW - 30 * i, "type": "denied", "consumer": "bad-bot",
            "tool": INVOICE, "status": "denied", "latency_ms": 0, "detail": "not_granted", "client_ip": "10.9.9.9"})


seed()

# ── overview ──────────────────────────────────────────────────────────────────
print("overview KPIs")
ov = analytics.overview(86400, now=NOW)
k = ov["kpis"]
check("total counts all attempts", k["total"] == 12)
check("ok/denied/error split", (k["ok"], k["denied"], k["error"]) == (6, 4, 2))
check("auth_rate = ok/total", k["auth_rate"] == 50.0)
check("rows summed (errors contribute 0)", k["rows"] == 60)
check("redactions counted from detail", k["redactions"] == 6)
check("sensitive-capable calls counted", k["sensitive"] >= 8)
check("avg latency over ok calls", k["avg_latency"] == 100.0)

print("overview series buckets by range")
check("24h -> 24 hourly buckets", len(ov["series"]) == 24)
check("7d -> 7 daily buckets", len(analytics.overview(7 * 86400, now=NOW)["series"]) == 7)
check("30d -> 30 daily buckets", len(analytics.overview(30 * 86400, now=NOW)["series"]) == 30)

print("overview heatmap + sensitivity helper")
hm = ov["heatmap"]
check("heatmap grid is 7x24", len(hm["grid"]) == 7 and all(len(r) == 24 for r in hm["grid"]))
check("heatmap max reflects busiest cell", hm["max"] >= 6)
check("sensitive detection: orders tool is sensitive", analytics._is_sensitive_call({"tool": ORDERS}) is True)
check("sensitive detection: unknown tool is not", analytics._is_sensitive_call({"tool": "bogus"}) is False)

# ── alerts / incidents ────────────────────────────────────────────────────────
print("alerts grouping + severity")
al = {a["id"]: a for a in analytics.alerts(now=NOW)}
check("two incidents grouped", len(al) == 2)
check("enumeration is critical", al.get("ara-bot:enumeration", {}).get("severity") == "critical")
check("denials is high", al.get("bad-bot:denials", {}).get("severity") == "high")
check("enumeration counts events", al.get("ara-bot:enumeration", {}).get("events", 0) >= 6)
check("all incidents open initially", all(a["status"] == "open" for a in al.values()))
check("alerts carry a consumer_id field for one-click containment", all("consumer_id" in a for a in al.values()))

print("durable alert status (reconstructed from the audit log)")
audit._records.append({"ts": NOW, "type": "policy_change", "consumer": "caleb",
    "tool": "alert_ack ara-bot:enumeration", "status": "changed", "detail": None})
al2 = {a["id"]: a for a in analytics.alerts(now=NOW)}
check("acked incident reads acknowledged", al2["ara-bot:enumeration"]["status"] == "acknowledged")
check("acked incident records who", al2["ara-bot:enumeration"]["handled_by"] == "caleb")
check("other incident stays open", al2["bad-bot:denials"]["status"] == "open")

# ── backend health (traffic-inferred; no live probe) ──────────────────────────
print("backend health (probe disabled)")
import asyncio  # noqa: E402
bh = {b["backend"]: b for b in asyncio.run(analytics.backends_health(now=NOW, probe=False))}
check("orders backend healthy from traffic", bh["minierp_orders"]["status"] == "healthy")
check("orders error_rate computed", bh["minierp_orders"]["error_rate"] == 0.25)
check("finance idle (denials aren't ok/error traffic)", bh["minierp_finance"]["status"] == "idle")
check("no probe -> probe_ok is None", bh["minierp_orders"]["probe_ok"] is None)

# ── request-level caching + invalidation ──────────────────────────────────────
print("request-level caching")
o1 = analytics.overview_cached(86400)
check("overview_cached memoizes within TTL", analytics.overview_cached(86400) is o1)
analytics.invalidate_cache("overview")
check("invalidate_cache forces recompute", analytics.overview_cached(86400) is not o1)
a1 = analytics.alerts_cached()
audit._records.append({"ts": NOW, "type": "policy_change", "consumer": "caleb",
    "tool": "alert_resolve bad-bot:denials", "status": "changed", "detail": None})
check("alerts stay cached until invalidated", analytics.alerts_cached() is a1)
analytics.invalidate_cache("alerts")
_res = [x for x in analytics.alerts_cached() if x["id"] == "bad-bot:denials"]
check("after invalidation, resolve is reflected", bool(_res) and _res[0]["status"] == "resolved")

# ── per-consumer risk profile + least privilege ───────────────────────────────
print("consumer risk profile + least-privilege")
try:
    from store import get_store
    from store.models import ConsumerRecord
    from policy.categories import Category
    store = get_store()
    if not store.writable:
        raise RuntimeError("store not writable")
    # An explicit 3-tool grant so "unused" is deterministic (not seed-dependent).
    store.upsert_category(Category(id="orders", display_name="Orders", backend="minierp_orders",
        tools=frozenset({"get_customer_orders", "get_customer_order_total", "get_order_details"}),
        levels=frozenset()))
    store.upsert_consumer(ConsumerRecord(consumer_id="ara-bot", name="ara-bot", key_hash="x",
        status="active", categories=["orders"], department="support",
        key_created_at=NOW - 100 * 86400, key_rotated_at=NOW - 100 * 86400))

    p = analytics.consumer_profile("ara-bot")
    check("profile resolves", p is not None)
    check("distinct customers counted", p["distinct_customers"] == 6)
    check("rows returned summed", p["rows_returned"] == 60)
    check("used tool recorded", ORDERS in p["used_tools"])
    check("granted includes the used tool", ORDERS in p["granted_tools"])
    check("used tool not flagged as unused", ORDERS not in p["unused_grants"])
    check("least-privilege finds the 2 unused grants", len(p["unused_grants"]) == 2)
    dep = {d["k"]: d["n"] for d in analytics.overview(86400, now=NOW)["by_department"]}
    check("by_department maps consumer -> department", dep.get("support") == 8)

    hy = {c["name"]: c for c in analytics.credential_hygiene()}
    check("hygiene uses persisted key age", 99 <= (hy["ara-bot"]["key_age_days"] or 0) <= 101)
    check("hygiene flags a >90d-old key", "key >90d old" in hy["ara-bot"]["flags"])

    # break-glass controls round-trip through the store
    check("controls default off", store.get_controls()["paused_agents"] is False)
    store.set_controls({"paused_agents": True, "paused_backends": ["minierp_finance", "bogus"]})
    ctrl = store.get_controls()
    check("controls persist paused_agents", ctrl["paused_agents"] is True)
    check("controls persist paused_backends (bogus kept as-is by store)", "minierp_finance" in ctrl["paused_backends"])
except Exception as exc:
    check(f"store-backed profile checks ran (got: {exc})", False)
finally:
    try:
        _STORE_FILE.unlink()
    except OSError:
        pass

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
