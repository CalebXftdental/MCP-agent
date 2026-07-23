"""Document review records for governed office artifact collaboration."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DocumentReviewComment:
    comment_id: str
    author: str
    body: str
    anchor: str = ""
    created_at: float = 0.0

    def public_dict(self) -> dict:
        return {
            "commentId": self.comment_id,
            "author": self.author,
            "body": self.body,
            "anchor": self.anchor,
            "createdAt": self.created_at,
        }


@dataclass(frozen=True)
class DocumentReviewSession:
    review_id: str
    artifact_id: str
    artifact_version_id: str
    requested_by: str
    owner: str
    status: str = "open"
    provider: str = "local"
    editor_url: str = ""
    reason: str = ""
    decision: str = ""
    decided_by: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    decided_at: float | None = None
    comments: list[DocumentReviewComment] = field(default_factory=list)

    def public_dict(self) -> dict:
        return {
            "reviewId": self.review_id,
            "artifactId": self.artifact_id,
            "artifactVersionId": self.artifact_version_id,
            "requestedBy": self.requested_by,
            "owner": self.owner,
            "status": self.status,
            "provider": self.provider,
            "editorUrl": self.editor_url,
            "reason": self.reason,
            "decision": self.decision,
            "decidedBy": self.decided_by,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "decidedAt": self.decided_at,
            "comments": [c.public_dict() for c in self.comments],
            "commentCount": len(self.comments),
        }