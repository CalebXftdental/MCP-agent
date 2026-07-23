"""Artifact records for generated office outputs.

Artifacts are durable files produced by governed tools or workflows. The binary
file lives in the artifact store; this metadata is what the gateway can list,
authorize, audit, and eventually use for approvals/retention.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ArtifactVersionRecord:
    artifact_id: str
    version_id: str
    version_number: int
    filename: str
    storage_path: str
    mime_type: str
    checksum: str = ""
    size_bytes: int = 0
    created_by: str = ""
    created_at: float = 0.0
    note: str = ""

    def public_dict(self) -> dict:
        return {
            "artifactId": self.artifact_id,
            "versionId": self.version_id,
            "versionNumber": self.version_number,
            "filename": self.filename,
            "mimeType": self.mime_type,
            "checksum": self.checksum,
            "sizeBytes": self.size_bytes,
            "createdBy": self.created_by,
            "createdAt": self.created_at,
            "note": self.note,
            "downloadUrl": f"/artifacts/{self.artifact_id}/versions/{self.version_id}/download",
        }


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    owner: str
    title: str
    filename: str
    type: str
    storage_path: str
    mime_type: str
    classification: list[str] = field(default_factory=list)
    source_workflow_run_id: str = ""
    source_tool_calls: list[str] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    checksum: str = ""
    size_bytes: int = 0
    status: str = "ready"
    created_at: float = 0.0
    expires_at: float | None = None
    retention_days: int | None = None
    current_version: int = 1
    version_count: int = 1
    latest_version_id: str = "v1"

    def public_dict(self) -> dict:
        return {
            "artifactId": self.artifact_id,
            "owner": self.owner,
            "title": self.title,
            "filename": self.filename,
            "type": self.type,
            "mimeType": self.mime_type,
            "classification": list(self.classification),
            "sourceWorkflowRunId": self.source_workflow_run_id,
            "sourceToolCalls": list(self.source_tool_calls),
            "sourceArtifactIds": list(self.source_artifact_ids),
            "checksum": self.checksum,
            "sizeBytes": self.size_bytes,
            "status": self.status,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "retentionDays": self.retention_days,
            "currentVersion": self.current_version,
            "versionCount": self.version_count,
            "latestVersionId": self.latest_version_id,
            "downloadUrl": f"/artifacts/{self.artifact_id}/download",
            "workbenchUrl": f"/artifacts/{self.artifact_id}/workbench",
        }
