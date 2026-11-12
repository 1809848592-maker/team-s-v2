from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ResubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_plan_version_id: UUID
    expected_spec_version_id: UUID


class ResubmissionRequestInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    requested_by: UUID
    requested_at: datetime
    target_plan_version_id: UUID
    target_spec_version_id: UUID


class VersionBindingInfo(BaseModel):
    bound_plan_version: int
    bound_spec_version: int
    latest_plan_version_id: UUID
    latest_spec_version_id: UUID
    latest_plan_version: int
    latest_spec_version: int
    plan_outdated: bool
    spec_outdated: bool
    based_on_old_version: bool
    can_request_resubmission: bool
    resubmission_request: ResubmissionRequestInfo | None = None
