"""项目级衔接审核的只读响应；冻结原文和租约标识不进入公开接口。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class LinkageProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str
    instruction_version: str
    model: str
    input_hash: str


class LinkageModule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module_id: UUID
    module_name: str
    delivery_id: UUID | None
    delivery_version: int | None
    plan_version_id: UUID | None
    spec_version_id: UUID | None
    plan_outdated: bool
    spec_outdated: bool
    state: Literal["summarized", "uncovered", "failed"]
    reason: str | None
    summary: str | None
    provenance: LinkageProvenance | None
    source_segments: list["LinkageSourceSegment"] = Field(default_factory=list)


class LinkageSourceSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_id: str
    location: dict


class LinkageFinding(LinkageProvenance):
    id: str
    check_key: str
    category: Literal["project_linkage"]
    severity: Literal["important", "suggestion"]
    module_ids: list[UUID]
    problem: str
    excerpt: str
    basis: str
    suggestion: str
    evidence_status: Literal["verified", "unverified"]
    request_id: UUID | None


class LinkageComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["completed", "uncovered", "incomplete"]
    provenance: LinkageProvenance | None
    checks: list["LinkageCheck"] = Field(default_factory=list)


class LinkageCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Literal["connection", "overlap", "omission", "contradiction", "plan_alignment"]
    status: Literal["issues", "checked", "uncovered"]
    reason: str


class LinkageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage: Literal["completed", "incomplete"]
    coverage_notes: list[str]
    modules: list[LinkageModule]
    findings: list[LinkageFinding]
    discarded_findings: int = Field(ge=0)
    comparison: LinkageComparison


class ProjectLinkageInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    project_id: UUID
    requested_by: UUID
    plan_version_id: UUID
    plan_version: int
    spec_version_id: UUID
    spec_version: int
    status: Literal["queued", "running", "completed", "partial_failed"]
    attempt: int
    max_attempts: int
    error_code: str | None
    message: str
    created_at: datetime
    completed_at: datetime | None
    can_retry: bool
    can_convert: bool
    result: LinkageResult | None
