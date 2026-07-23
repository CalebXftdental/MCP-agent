"""Approval records for consequence-bearing office actions."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    requested_by: str
    reason: str
    status: str = "pending"
    risk_level: str = "medium"
    artifact_ids: list[str] = field(default_factory=list)
    workflow_run_id: str = ""
    approver: str = ""
    decision_note: str = ""
    created_at: float = 0.0
    decided_at: float | None = None

    def public_dict(self) -> dict:
        return {
            "approvalId": self.approval_id,
            "requestedBy": self.requested_by,
            "reason": self.reason,
            "status": self.status,
            "riskLevel": self.risk_level,
            "artifactIds": list(self.artifact_ids),
            "workflowRunId": self.workflow_run_id,
            "approver": self.approver,
            "decisionNote": self.decision_note,
            "createdAt": self.created_at,
            "decidedAt": self.decided_at,
        }
