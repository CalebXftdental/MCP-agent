"""Sends the signup verification-code email via Microsoft Graph, using the
GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET / GRAPH_SENDER_EMAIL app
registration (copied from the email_agent project's realbackend.env into this
repo's own .env -- see gateway/.env.local.example).

Unlike rerank_client.py's fail-soft shape, this raises on any failure: a signup
code that silently never sent is worse than a signup that visibly errors out and
lets the user retry.
"""
from __future__ import annotations

import os

import httpx

_TOKEN_URL_FMT = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_SEND_MAIL_URL_FMT = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
_SCOPE = "https://graph.microsoft.com/.default"


class GraphMailerError(RuntimeError):
    """Raised when Graph auth or sendMail fails -- caller surfaces this as a 502,
    not a swallowed "check your email"."""


def _timeout() -> float:
    return float(os.getenv("GRAPH_MAILER_TIMEOUT_SEC") or "15")


async def _access_token(client: httpx.AsyncClient) -> str:
    tenant = os.getenv("GRAPH_TENANT_ID") or ""
    client_id = os.getenv("GRAPH_CLIENT_ID") or ""
    client_secret = os.getenv("GRAPH_CLIENT_SECRET") or ""
    if not tenant or not client_id or not client_secret:
        raise GraphMailerError("Graph credentials are not configured (GRAPH_TENANT_ID / "
                               "GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET)")
    resp = await client.post(
        _TOKEN_URL_FMT.format(tenant=tenant),
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": _SCOPE,
        },
        timeout=_timeout(),
    )
    if resp.status_code != 200:
        raise GraphMailerError(f"Graph token request failed ({resp.status_code})")
    token = resp.json().get("access_token")
    if not token:
        raise GraphMailerError("Graph token response had no access_token")
    return token


async def _send(to_addr: str, subject: str, body: str) -> None:
    sender = os.getenv("GRAPH_SENDER_EMAIL") or ""
    if not sender:
        raise GraphMailerError("GRAPH_SENDER_EMAIL is not configured")
    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to_addr}}],
        },
        "saveToSentItems": False,
    }
    async with httpx.AsyncClient() as client:
        token = await _access_token(client)
        resp = await client.post(
            _SEND_MAIL_URL_FMT.format(sender=sender),
            headers={"Authorization": f"Bearer {token}"},
            json=message,
            timeout=_timeout(),
        )
    if resp.status_code not in (200, 202):
        raise GraphMailerError(f"Graph sendMail failed ({resp.status_code}): {resp.text[:300]}")


async def send_verification_email(to_addr: str, code: str) -> None:
    await _send(
        to_addr,
        "Your Frontier MCP Workspace verification code",
        f"Your verification code is {code}. It expires in "
        f"{int(os.getenv('GOVERNANCE_SIGNUP_CODE_TTL_SEC') or '600') // 60} minutes. "
        "If you didn't request this, you can ignore this email.",
    )


async def send_account_approved_email(to_addr: str, full_name: str, login_url: str) -> None:
    """Best-effort -- see send_admin_signup_notification's docstring on why a
    raised GraphMailerError here shouldn't undo or fail the approve action
    itself (gateway/backend/admin_policy.py::_admin_request_approve)."""
    greeting = f"Hi {full_name}," if full_name else "Hi,"
    link_line = f"Sign in here: {login_url}" if login_url else "Sign in from the workspace you registered at."
    await _send(
        to_addr,
        "Your Frontier MCP Workspace account is approved",
        f"{greeting}\n\nYour account has been approved and is ready to use.\n\n{link_line}",
    )


async def send_admin_signup_notification(full_name: str, username: str, email: str, department: str) -> None:
    """Best-effort heads-up to whoever reviews signups (SIGNUP_ADMIN_NOTIFY_EMAIL) --
    the Pending Signups admin panel is still the source of truth; this just saves
    them from having to poll it. Caller should treat a raised GraphMailerError here
    as non-fatal (see gateway/backend/session.py::_signup_verify_code) -- the
    account is already created and queued either way."""
    to_addr = os.getenv("SIGNUP_ADMIN_NOTIFY_EMAIL") or ""
    if not to_addr:
        return
    await _send(
        to_addr,
        "New signup pending approval",
        f"{full_name} ({username}, {email}) signed up for the {department} department and is "
        "waiting on admin approval in Pending Signups.",
    )
