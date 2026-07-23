"""Auth primitive tests: passwords, session tokens, lockout (offline, no servers).

Run:  python _smoke/test_auth.py
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "governance_core"))

os.environ["GOVERNANCE_SESSION_SECRET"] = "test-secret-please-change"
os.environ["GOVERNANCE_LOGIN_MAX_FAILURES"] = "3"

from auth.passwords import hash_password, verify_password
from auth.session import issue_session, verify_session
from auth import lockout

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {name}")
    else:
        FAIL += 1; print(f"  FAIL {name}")


print("passwords (scrypt)")
h = hash_password("hunter2")
check("hash has scrypt scheme", h.startswith("scrypt$"))
check("hash is not the password", "hunter2" not in h)
check("verify true for right password", verify_password("hunter2", h))
check("verify false for wrong password", not verify_password("hunter3", h))
check("verify false for empty stored", not verify_password("x", None))
check("two hashes of same pw differ (random salt)", hash_password("hunter2") != hash_password("hunter2"))

print("session tokens")
tok = issue_session("login:alice", "alice", "admin", ttl_sec=60)
claims = verify_session(tok)
check("valid token verifies", claims is not None and claims["name"] == "alice" and claims["role"] == "admin")
check("tampered token rejected", verify_session(tok[:-3] + "aaa") is None)
check("garbage token rejected", verify_session("not.a.token") is None)
check("empty token rejected", verify_session("") is None)
expired = issue_session("login:bob", "bob", "user", ttl_sec=-1)
check("expired token rejected", verify_session(expired) is None)
# a different secret must not validate a token signed with ours
_saved = os.environ["GOVERNANCE_SESSION_SECRET"]
os.environ["GOVERNANCE_SESSION_SECRET"] = "different-secret"
check("token signed with other secret rejected", verify_session(tok) is None)
os.environ["GOVERNANCE_SESSION_SECRET"] = _saved

print("lockout (max 3)")
k = "alice|1.2.3.4"
lockout.reset(k)
check("not locked initially", not lockout.is_locked(k))
lockout.record_failure(k); lockout.record_failure(k)
check("not locked after 2", not lockout.is_locked(k))
tripped = lockout.record_failure(k)
check("3rd failure trips lockout", tripped and lockout.is_locked(k))
lockout.reset(k)
check("reset clears lockout", not lockout.is_locked(k))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
