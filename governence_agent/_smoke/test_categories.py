"""Offline category-model tests: resolve() union + decide() (no servers, no deps).

Run:  python _smoke/test_categories.py
Proves: category templates (1 category = 1 data-domain backend) -> effective grant;
UNION across a principal's categories; deny-by-default tool authz; per-category
redaction; overrides + precedence; the category-less legacy fallback.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "governance_core"))

from policy import categories
from policy.decision import Scope, decide
from policy.resolve import resolve

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {name}")
    else:
        FAIL += 1; print(f"  FAIL {name}")


class Rec:
    def __init__(self, categories=None, overrides=None, allowed_levels=frozenset()):
        self.categories = categories or []
        self.overrides = overrides or {}
        self.allowed_levels = allowed_levels


def grant_for(cats=None, overrides=None, allowed_levels=frozenset()):
    return resolve(Rec(cats, overrides, allowed_levels), categories.get_category)


print("category -> backend grant")
orders = grant_for(["orders"])
check("orders grants orders-backend tool", orders.allows_tool("minierp_orders", "get_customer_orders"))
check("orders does NOT grant accounts tool", not orders.allows_tool("minierp_accounts", "get_contacts"))
check("orders levels: SENSITIVE (totals)", "SENSITIVE" in orders.levels_for("minierp_orders"))
check("orders levels: no PII", "PII" not in orders.levels_for("minierp_orders"))

print("UNION across multiple categories")
both = grant_for(["orders", "accounts"])
check("union grants orders tool", both.allows_tool("minierp_orders", "get_customer_orders"))
check("union grants accounts tool", both.allows_tool("minierp_accounts", "get_contacts"))
check("orders backend keeps SENSITIVE", "SENSITIVE" in both.levels_for("minierp_orders"))
check("accounts backend has PII", "PII" in both.levels_for("minierp_accounts"))
check("no leakage: orders backend has no PII", "PII" not in both.levels_for("minierp_orders"))

print("deny-by-default via PDP")
d = decide("bot", "get_contacts", {}, Scope(customer_id="X"), grant=orders)
check("orders-only denied accounts tool (not_granted)", d.allowed is False and d.reason == "not_granted")
a = decide("bot", "get_customer_orders", {}, Scope(customer_id="X"), grant=orders)
check("orders-only allowed orders tool", a.allowed is True)

print("per-category redaction")
# orders has no PII, but orders tools have no PII fields; check total (SENSITIVE) kept
v = decide("bot", "get_customer_orders", {}, Scope(customer_id="X"), grant=orders)
check("orders: total NOT redacted (has SENSITIVE)", not any(r.field == "total" for r in v.redaction_plan))
# shipments has neither SENSITIVE nor PII -> orderTotal on shipping-by-order redacted
ship = grant_for(["shipments"])
vs = decide("bot", "get_shipping_by_order", {}, Scope(), grant=ship)
check("shipments allowed shipping tool", vs.allowed)
check("shipments: orderTotal redacted (no SENSITIVE)", any(r.field == "orderTotal" for r in vs.redaction_plan))
# accounts has PII -> contacts email kept
acct = grant_for(["accounts"])
va = decide("bot", "get_contacts", {}, Scope(customer_id="X"), grant=acct)
check("accounts: email NOT redacted (has PII)", not any(r.field == "email" for r in va.redaction_plan))

print("overrides + precedence")
plus = grant_for(["shipments"], {"minierp_finance": {"grantTools": ["get_invoice_details"]}})
check("override grants a tool on another backend", plus.allows_tool("minierp_finance", "get_invoice_details"))
minus = grant_for(["orders"], {"minierp_orders": {"denyTools": ["get_customer_orders"]}})
check("override deny removes a category-granted tool", not minus.allows_tool("minierp_orders", "get_customer_orders"))
elev = grant_for(["shipments"], {"minierp_shipments": {"grantLevels": ["SENSITIVE"]}})
check("override can add a level", "SENSITIVE" in elev.levels_for("minierp_shipments"))

print("unknown category -> contributes nothing (fail closed)")
unk = grant_for(["no_such_category"])
check("unknown category grants nothing", not unk.allows_tool("minierp_orders", "get_customer_orders"))

print("category-less legacy fallback -> allow-all + explicit levels")
legacy = grant_for(None, allowed_levels=frozenset({"PUBLIC", "INTERNAL", "PII", "SENSITIVE"}))
check("legacy allows any tool", legacy.allows_tool("minierp_orders", "get_customer_orders")
      and legacy.allows_tool("minierp_finance", "get_gl_account_transactions"))
check("legacy uses explicit levels", "SENSITIVE" in legacy.levels_for("minierp_orders"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
