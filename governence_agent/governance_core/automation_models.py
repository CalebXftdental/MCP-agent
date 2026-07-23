"""Automation schedules for recurring workflow jobs."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AutomationSchedule:
    automation_id: str
    owner: str
    template_id: str
    display_name: str
    actor_type: str = "user"
    agent_id: str = ""
    inputs: dict = field(default_factory=dict)
    interval_sec: int = 86400
    status: str = "active"
    created_at: float = 0.0
    updated_at: float = 0.0
    next_run_at: float = 0.0
    last_run_at: float | None = None
    last_run_id: str = ""
    last_status: str = "never_run"
    run_count: int = 0

    def public_dict(self) -> dict:
        return {
            "automationId": self.automation_id,
            "owner": self.owner,
            "templateId": self.template_id,
            "actorType": self.actor_type,
            "agentId": self.agent_id,
            "displayName": self.display_name,
            "inputs": dict(self.inputs),
            "intervalSec": self.interval_sec,
            "status": self.status,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "nextRunAt": self.next_run_at,
            "lastRunAt": self.last_run_at,
            "lastRunId": self.last_run_id,
            "lastStatus": self.last_status,
            "runCount": self.run_count,
        }
