"""Offline PDP + redaction tests -- no servers, no third-party deps.

Run:  python _smoke/test_policy.py
Proves the deterministic governance core: deny rules, and per-consumer field
censorship (the same tool result redacted differently for different requesters).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "governance_core"))

from policy import manifest
from policy.decision import Scope, decide
from policy.redaction import apply as apply_redaction

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


# A canonical contacts result as mcp-minierp would return it.
CONTACTS = {
    "source": "miniERP", "status": "success", "intent": "account_contacts",
    "customerId": "PRIN100",
    "records": [
        {"contactId": 1, "name": "Jane Doe", "displayName": "Frontier Dental",
         "type": "Billing", "email": "jane@frontier.com", "phone": "555-1212"},
    ],
}
ORDERS = {
    "source": "miniERP", "status": "success", "intent": "customer_orders",
    "customerId": "PRIN100",
    "records": [{"orderNumber": "SO123", "status": "Open", "statusCode": "N",
                 "total": 1999.50, "date": "2026-01-01"}],
}


def redact_for(consumer, tool, result):
    v = decide(consumer, tool, {}, Scope(customer_id="PRIN100"))
    assert v.allowed, f"expected allow for {consumer}/{tool}"
    out, touched = apply_redaction(v.redaction_plan, result)
    return out, touched


print("namespacing")
check("namespaced()", manifest.namespaced("get_customer_orders") == "minierp_orders_get_customer_orders")
check("canonical() round-trip", manifest.canonical("minierp_orders_get_customer_orders") == "get_customer_orders")
check("canonical() unknown -> None", manifest.canonical("bogus_tool") is None)

print("deny rules")
check("unknown tool denied", decide("chatbot", "no_such_tool", {}, Scope()).allowed is False)
d = decide("chatbot", "get_customer_orders", {}, Scope(customer_id=None))
check("account-scoped w/o scope denied", d.allowed is False and d.reason == "missing_customer_scope")
check("account-scoped w/o scope asks for customerId", d.missing_fields == ["customerId"])
check("account-scoped WITH scope allowed", decide("chatbot", "get_customer_orders", {}, Scope(customer_id="PRIN100")).allowed)
check("non-scoped tool allowed w/o scope", decide("chatbot", "get_order_details", {}, Scope()).allowed)

print("per-consumer censorship: contacts (PII fields)")
chatbot, t1 = redact_for("chatbot", "get_contacts", CONTACTS)
check("chatbot keeps email", chatbot["records"][0].get("email") == "jane@frontier.com")
check("chatbot keeps phone", chatbot["records"][0].get("phone") == "555-1212")

analytics, t2 = redact_for("analytics", "get_contacts", CONTACTS)
check("analytics email masked", analytics["records"][0].get("email") == "j***@frontier.com")
check("analytics phone masked", analytics["records"][0].get("phone") == "5***")
check("analytics name masked (PII)", analytics["records"][0].get("name") == "J***")
check("analytics keeps displayName (INTERNAL)", analytics["records"][0].get("displayName") == "Frontier Dental")
check("analytics audit lists redacted fields", set(t2) == {"name", "email", "phone"})

email_bot, _ = redact_for("email_bot", "get_contacts", CONTACTS)
check("email_bot keeps email (PII entitled)", email_bot["records"][0].get("email") == "jane@frontier.com")

print("per-consumer censorship: orders (SENSITIVE total)")
cb_orders, _ = redact_for("chatbot", "get_customer_orders", ORDERS)
check("chatbot keeps total", cb_orders["records"][0].get("total") == 1999.50)

eb_orders, teb = redact_for("email_bot", "get_customer_orders", ORDERS)
check("email_bot total dropped (no SENSITIVE)", "total" not in eb_orders["records"][0])
check("email_bot keeps orderNumber (INTERNAL)", eb_orders["records"][0].get("orderNumber") == "SO123")
check("email_bot audit lists total", teb == ["total"])

an_orders, _ = redact_for("analytics", "get_customer_orders", ORDERS)
check("analytics keeps total (SENSITIVE entitled)", an_orders["records"][0].get("total") == 1999.50)

default_orders, _ = redact_for("unknown_consumer", "get_customer_orders", ORDERS)
check("unknown consumer -> default least-privilege drops total", "total" not in default_orders["records"][0])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
