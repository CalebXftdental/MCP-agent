"""Credential hashing.

API keys are HIGH-ENTROPY random secrets, so a fast cryptographic hash (SHA-256)
with an optional server-side pepper is the correct choice -- NOT a slow
password-hash like argon2, which would tax every MCP request for no security gain
(there is nothing to brute-force in a 256-bit random key). Human LOGIN passwords
are low-entropy and DO use argon2 -- that lives with the login work (step 3), not
here.

We store only `hash_api_key(key)`; the plaintext key is shown once at issuance and
never persisted. Verification is constant-time.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets


def _pepper() -> str:
    # Optional shared secret mixed into the hash so a leaked store alone can't be
    # used to verify keys. Set GOVERNANCE_KEY_PEPPER in Key Vault in prod; empty in dev.
    return os.getenv("GOVERNANCE_KEY_PEPPER", "")


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256((_pepper() + api_key).encode("utf-8")).hexdigest()


def verify_api_key(api_key: str, key_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(api_key), key_hash or "")


def generate_api_key(prefix: str = "gmk") -> str:
    """Mint a new high-entropy key. Shown once; only its hash is stored."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"
