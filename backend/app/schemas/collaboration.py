from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

ReportStage = Literal["not_started", "planning", "writing", "checking"]
EffectiveStage = Literal["not_started", "planning", "writing", "checking", "submitted"]
RequestStatus = Literal["open", "accepted", "responded", "resolved"]
RequiredText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CollaborationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*")
    @classmethod
    def reject_null_character(cls, value):
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("文字不能包含空字符")
        return value


class ProgressCreate(CollaborationSchema):
    stage: ReportStage
    summary: RequiredText = Field(max_length=1000)


class ProgressInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    module_id: UUID
    user_id: UUID
    stage: ReportStage
    summary: str
    plan_version_id: UUID
    created_at: datetime
    source: Literal["human"] = "human"


class ModuleProgress(BaseModel):
    module_id: UUID
    stage: EffectiveStage
    last_report: ProgressInfo | None = None
    delivery_id: UUID | None = None


class RequestCreate(CollaborationSchema):
    title: RequiredText = Field(max_length=200)
    content: RequiredText = Field(max_length=10000)
    target_user_id: UUID | None = None
    target_module_id: UUID | None = None
    expected_by: AwareDatetime | None = None

    @field_validator("expected_by")
    @classmethod
    def utc_expected_by(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def require_one_target(self):
        if (self.target_user_id is None) == (self.target_module_id is None):
            raise ValueError("协作需求必须且只能指定一名成员或一个模块")
        return self


class MessageCreate(CollaborationSchema):
    content: RequiredText = Field(max_length=10000)


class RequestMessageInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    request_id: UUID
    author_id: UUID
    kind: Literal["response", "supplement"]
    content: str
    created_at: datetime


class RequestInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    created_by: UUID
    title: str
    content: str
    target_user_id: UUID | None
    target_module_id: UUID | None
    expected_by: datetime | None
    status: RequestStatus
    accepted_by: UUID | None
    created_at: datetime
    accepted_at: datetime | None
    responded_at: datetime | None
    resolved_at: datetime | None
    messages: list[RequestMessageInfo] = Field(default_factory=list)
