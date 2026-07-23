"""Store unit tests -- key hashing + env seeding + auth resolution + PDP via levels.

Run:  python _smoke/test_store.py     (no servers, no third-party deps)
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "governance_core"))

# Seed env BEFORE importing the store (LocalPolicyStore.from_env reads it).
os.environ["GOVERNANCE_KEY_CHATBOT"] = "dev-chatbot-key"
os.environ["GOVERNANCE_KEY_ANALYTICS"] = "dev-analytics-key"
os.environ["GOVERNANCE_RATE_LIMIT_CHATBOT"] = "1234"

import store as store_pkg
from store.keys import generate_api_key, hash_api_key, verify_api_key
from policy.decision import Scope, decide

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {name}")
    else:
        FAIL += 1; print(f"  FAIL {name}")


print("key hashing")
k = generate_api_key()
h = hash_api_key(k)
check("hash is 64-hex sha256", len(h) == 64 and all(c in "0123456789abcdef" for c in h))
check("verify true for right key", verify_api_key(k, h))
check("verify false for wrong key", not verify_api_key(k + "x", h))
check("plaintext key never equals its hash", k != h)

print("env seeding + auth")
store_pkg.reset_store()
s = store_pkg.get_store()
names = sorted(c.name for c in s.consumers())
check("seeded chatbot + analytics", names == ["analytics", "chatbot"])
check("auth resolves by api key", (s.get_by_api_key("dev-chatbot-key") or None) and s.get_by_api_key("dev-chatbot-key").name == "chatbot")
check("auth rejects bad key", s.get_by_api_key("nope") is None)
check("per-consumer rate limit seeded", s.get_by_api_key("dev-chatbot-key").rate_limit_per_hour == 1234)
check("store carries entitlement levels", "PII" in s.get_by_api_key("dev-chatbot-key").allowed_levels)
check("analytics has no PII level", "PII" not in s.get_by_api_key("dev-analytics-key").allowed_levels)

print("PDP consumes store levels")
cb = s.get_by_api_key("dev-chatbot-key")
an = s.get_by_api_key("dev-analytics-key")
# chatbot (has PII) -> get_contacts email NOT in redaction plan
v_cb = decide("chatbot", "get_contacts", {}, Scope(customer_id="X"), levels=cb.allowed_levels)
check("chatbot: email not redacted", not any(r.field == "email" for r in v_cb.redaction_plan))
# analytics (no PII) -> email IS redacted
v_an = decide("analytics", "get_contacts", {}, Scope(customer_id="X"), levels=an.allowed_levels)
check("analytics: email redacted", any(r.field == "email" for r in v_an.redaction_plan))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
