"""Signs/verifies the JWT ONLYOFFICE Document Server expects on editor configs and
callback requests (same HS256-compact-JWT shape ONLYOFFICE itself uses).

Hand-rolled with stdlib hmac/hashlib, matching session.py's pattern, rather than
adding a PyJWT dependency for one call site. Format is a real compact JWT
(header.payload.signature, all base64url) since ONLYOFFICE's own client-side and
Document Server code expect that shape -- unlike session.py's cookie, this token
is read by an external service, not just verified here.

No secret configured => signing/verifying both no-op (return the payload
unsigned / None on verify), matching _onlyoffice_config's existing "ONLYOFFICE not
configured" gating rather than failing closed -- ONLYOFFICE integration is opt-in.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

_HEADER_B64 = base64.urlsafe_b64encode(
    json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8")
).decode("ascii").rstrip("=")


def _secret() -> bytes | None:
    secret = os.getenv("ONLYOFFICE_JWT_SECRET") or ""
    return secret.encode("utf-8") if secret else None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def enabled() -> bool:
    return _secret() is not None


def sign(payload: dict) -> str | None:
    """Return a compact HS256 JWT over `payload`, or None if no secret is set."""
    secret = _secret()
    if secret is None:
        return None
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{_HEADER_B64}.{payload_b64}".encode("ascii")
    sig_b64 = _b64(hmac.new(secret, signing_input, hashlib.sha256).digest())
    return f"{_HEADER_B64}.{payload_b64}.{sig_b64}"


def verify(token: str | None) -> dict | None:
    """Return the decoded payload if `token`'s signature checks out, else None.

    If no secret is configured, verification is a no-op failure (None) -- callers
    should only reach this once `enabled()` is true (i.e. ONLYOFFICE integration
    itself is turned on).
    """
    secret = _secret()
    if secret is None or not token or token.count(".") != 2:
        return None
    header_b64, payload_b64, sig_b64 = token.split(".")
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = _b64(hmac.new(secret, signing_input, hashlib.sha256).digest())
    if not hmac.compare_digest(sig_b64, expected):
        return None
    try:
        payload = json.loads(_unb64(payload_b64))
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def sign_access(artifact_id: str, action: str, ttl_sec: int = 3600) -> str | None:
    """Short-lived, artifact+action-scoped token for routes ONLYOFFICE's Document
    Server (or Document Builder) must fetch server-to-server, where there's no
    browser session cookie to check instead -- e.g. the artifact download URL
    embedded in an editor config, or a docbuilder script's source-file URL.
    Distinct from `sign()` (which signs a whole config/callback payload the
    caller already trusts structurally): this one carries its own expiry and
    exists purely as a bearer credential.
    """
    return sign({"aid": artifact_id, "action": action, "exp": int(time.time()) + ttl_sec})


def verify_access(token: str | None, artifact_id: str, action: str) -> bool:
    payload = verify(token)
    if payload is None:
        return False
    return (
        payload.get("aid") == artifact_id
        and payload.get("action") == action
        and int(payload.get("exp", 0)) >= int(time.time())
    )


def scoped_download_url(gateway_base: str, artifact_id: str, ttl_sec: int = 3600) -> str:
    """Absolute /artifacts/{id}/download URL, with an oo_token query param when a
    secret is configured (both the gateway's own config-builder and mcp-office's
    Document Builder tool need this same URL shape, hence living here)."""
    url = f"{gateway_base.rstrip('/')}/artifacts/{artifact_id}/download"
    token = sign_access(artifact_id, "download", ttl_sec)
    return f"{url}?oo_token={token}" if token else url
