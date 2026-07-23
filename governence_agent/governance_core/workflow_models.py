"""Workflow records for the office-assistant expansion scaffold."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WorkflowStep:
    step_id: str
    type: str
    status: str = "pending"
    tool: str = ""
    title: str = ""
    inputs: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class WorkflowTemplate:
    template_id: str
    display_name: str
    description: str
    required_categories: list[str] = field(default_factory=list)
    output_types: list[str] = field(default_factory=list)
    status: str = "draft"
    version: int = 1


@dataclass(frozen=True)
class WorkflowRun:
    run_id: str
    template_id: str
    requested_by: str
    status: str = "running"
    inputs: dict = field(default_factory=dict)
    steps: list[WorkflowStep] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    approval_ids: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    error: str | None = None
    resumed_at: float | None = None
