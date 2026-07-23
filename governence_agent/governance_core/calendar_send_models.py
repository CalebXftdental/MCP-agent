"""Calendar invite queue records for approval-gated external scheduling."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CalendarSendRecord:
    send_id: str
    owner: str
    draft_artifact_id: str
    approval_id: str
    status: str = "queued_for_connector"
    provider: str = "manual"
    title: str = ""
    start: str = ""
    end: str = ""
    timezone: str = "UTC"
    attendees: list[str] = field(default_factory=list)
    location: str = ""
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
            "title": self.title,
            "start": self.start,
            "end": self.end,
            "timezone": self.timezone,
            "attendees": list(self.attendees),
            "location": self.location,
            "message": self.message,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "sentAt": self.sent_at,
        }
