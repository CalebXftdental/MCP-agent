"""Autonomous agent profiles for governed scheduled work."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentProfile:
    agent_id: str
    display_name: str
    consumer_id: str
    status: str = "active"
    description: str = ""
    categories: list[str] = field(default_factory=list)
    allowed_template_ids: list[str] = field(default_factory=list)
    max_runs_per_day: int = 24
    created_by: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    last_run_at: float | None = None
    last_run_id: str = ""
    run_count: int = 0

    def public_dict(self) -> dict:
        return {
            "agentId": self.agent_id,
            "displayName": self.display_name,
            "consumerId": self.consumer_id,
            "status": self.status,
            "description": self.description,
            "categories": list(self.categories),
            "allowedTemplateIds": list(self.allowed_template_ids),
            "maxRunsPerDay": self.max_runs_per_day,
            "createdBy": self.created_by,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "lastRunAt": self.last_run_at,
            "lastRunId": self.last_run_id,
            "runCount": self.run_count,
        }
