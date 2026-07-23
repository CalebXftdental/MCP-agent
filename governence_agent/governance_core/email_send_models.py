"""Email send queue records for approval-gated external communication."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EmailSendRecord:
    send_id: str
    owner: str
    draft_artifact_id: str
    approval_id: str
    status: str = "queued_for_connector"
    provider: str = "manual"
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    subject: str = ""
    attachment_artifact_ids: list[str] = field(default_factory=list)
    message: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    sent_at: float | None = None

    def public_dict(self) -> dict:
        return {
            "sendId": self.send_id,
            "owner": self.owner,
            "draftArtifactId": self.draft_artifact_id,
            "approvalId": self.approval_id,
            "status": self.status,
            "provider": self.provider,
            "to": list(self.to),
            "cc": list(self.cc),
            "subject": self.subject,
            "attachmentArtifactIds": list(self.attachment_artifact_ids),
            "message": self.message,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "sentAt": self.sent_at,
        }
