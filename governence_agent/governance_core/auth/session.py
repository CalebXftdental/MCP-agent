"""Signed session tokens for the dashboard (stdlib HMAC-SHA256, JWT-like).

Format:  <b64url(payload_json)>.<b64url(hmac_sha256(secret, payload_b64))>
Payload: { sub, name, role, exp }.  Verification checks the signature in constant
time and the expiry. Signed with GOVERNANCE_SESSION_SECRET (a Key Vault secret in
prod). No secret configured => issuing and verifying both fail closed (login
disabled) rather than signing with an empty key.

Stateless by design: the cookie carries the claims, so a gateway restart doesn't
log everyone out and there is no server-side session store to scale. Trade-off: a
token is valid until it expires (short TTL); revocation-before-expiry would need a
denylist (out of scope for step 3).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

_DEFAULT_TTL_SEC = int(os.getenv("GOVERNANCE_SESSION_TTL_SEC") or str(60 * 60))


class SessionError(RuntimeError):
    pass


def _secret() -> bytes:
    secret = os.getenv("GOVERNANCE_SESSION_SECRET") or ""
    if not secret:
        raise SessionError("GOVERNANCE_SESSION_SECRET is not set; dashboard login is disabled")
    return secret.encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body_b64: str) -> str:
    return _b64(hmac.new(_secret(), body_b64.encode("ascii"), hashlib.sha256).digest())


def issue_session(consumer_id: str, name: str, role: str, ttl_sec: int | None = None) -> str:
    payload = {
        "sub": consumer_id,
        "name": name,
        "role": role,
        "exp": int(time.time()) + (ttl_sec or _DEFAULT_TTL_SEC),
    }
    body_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{body_b64}.{_sign(body_b64)}"


def verify_session(token: str | None) -> dict | None:
    """Return the claims dict if the token is valid and unexpired, else None."""
    if not token or "." not in token:
        return None
    body_b64, _, sig = token.partition(".")
    try:
        expected = _sign(body_b64)
    except SessionError:
        return None
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        claims = json.loads(_unb64(body_b64))
    except (ValueError, TypeError):
        return None
    if not isinstance(claims, dict) or int(claims.get("exp", 0)) < int(time.time()):
        return None
    return claims


def login_enabled() -> bool:
    return bool(os.getenv("GOVERNANCE_SESSION_SECRET"))
