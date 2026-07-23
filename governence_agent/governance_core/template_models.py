"""Reusable office/workflow template records for the governed assistant."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TemplateVersion:
    version: int
    created_by: str
    created_at: float
    content: dict = field(default_factory=dict)
    notes: str = ""

    def public_dict(self, include_content: bool = True) -> dict:
        data = {
            "version": self.version,
            "createdBy": self.created_by,
            "createdAt": self.created_at,
            "notes": self.notes,
        }
        if include_content:
            data["content"] = dict(self.content or {})
        return data


@dataclass(frozen=True)
class TemplateRecord:
    template_id: str
    display_name: str
    template_type: str
    status: str
    owner: str
    description: str = ""
    classification: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    allowed_workflow_ids: list[str] = field(default_factory=list)
    current_version: int = 1
    versions: list[TemplateVersion] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    def public_dict(self, include_versions: bool = False, include_content: bool = False) -> dict:
        latest = next((v for v in self.versions if v.version == self.current_version), None)
        data = {
            "templateId": self.template_id,
            "displayName": self.display_name,
            "templateType": self.template_type,
            "status": self.status,
            "owner": self.owner,
            "description": self.description,
            "classification": list(self.classification),
            "tags": list(self.tags),
            "allowedWorkflowIds": list(self.allowed_workflow_ids),
            "currentVersion": self.current_version,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "latestNotes": latest.notes if latest else "",
        }
        if include_versions:
            data["versions"] = [v.public_dict(include_content=include_content) for v in sorted(self.versions, key=lambda x: x.version)]
        elif include_content and latest:
            data["content"] = dict(latest.content or {})
        return data
