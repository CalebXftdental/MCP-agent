"""Share records for governed artifact access."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ArtifactShareRecord:
    share_id: str
    artifact_id: str
    owner: str
    shared_with: str
    permissions: list[str] = field(default_factory=list)
    created_by: str = ""
    status: str = "active"
    created_at: float = 0.0
    expires_at: float | None = None
    revoked_at: float | None = None
    revoked_by: str = ""

    def public_dict(self) -> dict:
        return {
            "shareId": self.share_id,
            "artifactId": self.artifact_id,
            "owner": self.owner,
            "sharedWith": self.shared_with,
            "permissions": list(self.permissions),
            "createdBy": self.created_by,
            "status": self.status,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "revokedAt": self.revoked_at,
            "revokedBy": self.revoked_by,
        }
