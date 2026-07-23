"""Read-only code automation plan records for governed developer assistance."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CodePlanRecord:
    plan_id: str
    owner: str
    request: str
    plan_type: str
    status: str
    risk_level: str
    summary: str
    proposed_files: list[str] = field(default_factory=list)
    proposed_commands: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    template_draft: dict = field(default_factory=dict)
    approval_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def public_dict(self, include_template: bool = True) -> dict:
        data = {
            "planId": self.plan_id,
            "owner": self.owner,
            "request": self.request,
            "planType": self.plan_type,
            "status": self.status,
            "riskLevel": self.risk_level,
            "summary": self.summary,
            "proposedFiles": list(self.proposed_files),
            "proposedCommands": list(self.proposed_commands),
            "findings": list(self.findings),
            "approvalId": self.approval_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "requiresApproval": True,
        }
        if include_template and self.template_draft:
            data["templateDraft"] = dict(self.template_draft)
        return data
