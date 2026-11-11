from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.review_jobs import ReviewProgress
from app.schemas.review_state import ReviewState

AIParticipation = Literal["none", "assisted", "primary"]


class DeliverySubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_participation: AIParticipation
    ai_note: str = Field(default="", max_length=2000)
    handoff_note: str = Field(default="", max_length=10000)

    @field_validator("*")
    @classmethod
    def reject_null_character(cls, value):
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("文字不能包含空字符")
        return value


class DeliveryVersionInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    module_id: UUID
    version: int
    filename: str
    sha256: str
    size: int
    submitted_by: UUID
    submitted_at: datetime
    plan_version_id: UUID
    spec_version_id: UUID
    ai_participation: AIParticipation
    ai_note: str
    handoff_note: str
    status: Literal["submitted"] = "submitted"
    review_status: ReviewState | None = None
    review_progress: ReviewProgress | None = None
    download_url: str = ""


class SharedMaterialInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    filename: str
    sha256: str
    size: int
    uploaded_by: UUID
    uploaded_at: datetime
    download_url: str = ""
