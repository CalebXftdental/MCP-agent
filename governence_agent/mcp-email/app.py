"""mcp-email -- governed email draft backend.

This first slice is draft-only. It creates durable email-draft artifacts but does
not send mail; future send tools should require approval for external recipients
and attachment classification checks.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.getenv("EMAIL_ENV_FILE") or ".env.local")

_CORE_DIR = str((Path(__file__).parent.parent / "governance_core").resolve())
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

import approval_store
import artifact_store
import email_send_store


def _allowed_hosts() -> list[str]:
    raw = (os.getenv("EMAIL_ALLOWED_HOSTS") or "").strip()
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    for h in raw.split(","):
        h = h.strip()
        if h:
            hosts.extend((h, f"{h}:*"))
    return hosts


mcp = FastMCP(
    "frontier-mcp-email",
    stateless_http=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts(),
        allowed_origins=["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*"],
    ),
)


def _classification(values: list[str] | None) -> list[str]:
    return sorted(set(values or ["INTERNAL"]))


def _internal_domains() -> set[str]:
    raw = (os.getenv("EMAIL_INTERNAL_DOMAINS") or "").strip().lower()
    return {d.strip().lstrip("@") for d in raw.split(",") if d.strip()}


def _all_recipients_internal(draft: dict) -> bool:
    """expansion.md §7.1 email_send_internal: internal-domain-only sends may skip
    the approval gate. Unset EMAIL_INTERNAL_DOMAINS (the default) means NOTHING
    is treated as internal, preserving today's always-approval-gated behavior."""
    domains = _internal_domains()
    if not domains:
        return False
    recipients = list(draft.get("to") or []) + list(draft.get("cc") or [])
    if not recipients:
        return False
    for addr in recipients:
        _, _, domain = str(addr).rpartition("@")
        if not domain or domain.strip().lower() not in domains:
            return False
    return True


@mcp.tool()
async def create_email_draft(
    owner: str,
    to: list[str],
    cc: list[str] | None = None,
    subject: str = "",
    body_markdown: str = "",
    attachment_artifact_ids: list[str] | None = None,
    classification: list[str] | None = None,
) -> str:
    """Create a durable email draft artifact. Does not send email."""
    draft = {
        "to": list(to or []),
        "cc": list(cc or []),
        "subject": subject,
        "bodyMarkdown": body_markdown,
        "body_markdown": body_markdown,
        "attachmentArtifactIds": list(attachment_artifact_ids or []),
        "attachment_artifact_ids": list(attachment_artifact_ids or []),
        "sendStatus": "draft",
    }
    payload = json.dumps(draft, indent=2).encode("utf-8")
    record = artifact_store.create_artifact(
        owner=owner,
        title=subject or "Email draft",
        filename="email-draft.json",
        payload=payload,
        artifact_type="email_draft",
        mime_type="application/json",
        classification=_classification(classification),
        source_artifact_ids=list(attachment_artifact_ids or []),
    )
    out = record.public_dict()
    out["artifactStatus"] = out.pop("status", "ready")
    return json.dumps({"source": "email", "status": "success", "draftId": record.artifact_id, **out})


@mcp.tool()
async def send_email_draft(owner: str, draft_id: str, approval_id: str = "") -> str:
    """Queue an approved email draft for the configured delivery connector. Skips
    the approval gate when every recipient is on an EMAIL_INTERNAL_DOMAINS domain
    (email_send_internal); everything else still needs an approved approval_id
    (email_send_external)."""
    record = artifact_store.get_artifact(draft_id)
    if record is None or record.type != "email_draft":
        return json.dumps({"source": "email", "status": "error", "errorCode": "draft_not_found", "draftId": draft_id})
    if record.owner != owner:
        return json.dumps({"source": "email", "status": "error", "errorCode": "forbidden", "draftId": draft_id})
    try:
        draft = json.loads(Path(record.storage_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return json.dumps({"source": "email", "status": "error", "errorCode": "draft_unreadable", "draftId": draft_id})
    internal_only = _all_recipients_internal(draft)
    if not internal_only:
        approval_store.reload_for_tests()
        approval = approval_store.get_approval(approval_id)
        if approval is None or approval.status != "approved" or draft_id not in approval.artifact_ids:
            return json.dumps({
                "source": "email",
                "status": "approval_required",
                "draftId": draft_id,
                "approvalId": approval_id,
                "message": "An approved approval record tied to this draft is required before queueing delivery.",
            })
    provider = (os.getenv("EMAIL_DELIVERY_PROVIDER") or "manual").strip().lower() or "manual"
    mock_send = (os.getenv("EMAIL_MOCK_SEND") or "false").strip().lower() in ("1", "true", "yes", "on")
    status = "sent" if mock_send else "queued_for_connector"
    message = "Mock delivery completed." if mock_send else "Queued for the configured email connector; no external delivery was attempted by this local adapter."
    send = email_send_store.create_send(
        owner=owner,
        draft_artifact_id=draft_id,
        approval_id=approval_id,
        provider=provider,
        to=list(draft.get("to") or []),
        cc=list(draft.get("cc") or []),
        subject=str(draft.get("subject") or ""),
        attachment_artifact_ids=list(draft.get("attachmentArtifactIds") or draft.get("attachment_artifact_ids") or []),
        status=status,
        message=message,
    )
    return json.dumps({
        "source": "email",
        "status": "success",
        "sendStatus": send.status,
        "sendId": send.send_id,
        "draftId": draft_id,
        "approvalId": approval_id,
        "provider": send.provider,
        "message": send.message,
    })


async def _health(_request):
    return JSONResponse({"ok": True, "service": "mcp-email"})


app = mcp.streamable_http_app()
app.add_route("/health", _health)


if __name__ == "__main__":
    port = int(os.getenv("PORT") or os.getenv("EMAIL_PORT") or "8040")
    print(f"[mcp-email] Starting on 0.0.0.0:{port} (path /mcp)")
    uvicorn.run(app, host="0.0.0.0", port=port)
