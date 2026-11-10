from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
RequiredText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PlanSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*")
    @classmethod
    def reject_null_character(cls, value):
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("文字不能包含空字符")
        return value


class TermDefinition(PlanSchema):
    term: Name
    definition: RequiredText = Field(max_length=5000)
    source: str = Field(default="", max_length=2000)


class DataDefinition(PlanSchema):
    variable: Name
    source: str = Field(default="", max_length=2000)
    processing: str = Field(default="", max_length=5000)


class ModuleDependency(PlanSchema):
    module_id: UUID
    needs: RequiredText = Field(max_length=5000)


class ModuleContent(PlanSchema):
    id: UUID = Field(default_factory=uuid4)
    name: Name
    assignee_ids: list[UUID] = Field(min_length=1, max_length=100)
    deadline: AwareDatetime | None = None
    goal: RequiredText = Field(max_length=10000)
    core_argument: str = Field(default="", max_length=10000)
    scope_in: str = Field(default="", max_length=10000)
    scope_out: str = Field(default="", max_length=10000)
    upstream: list[ModuleDependency] = Field(default_factory=list, max_length=200)
    downstream_handoff: str = Field(default="", max_length=10000)
    delivery_format: Literal["docx", "pptx", "pdf", "md", "other"]

    @field_validator("assignee_ids")
    @classmethod
    def unique_assignees(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("同一模块的负责人不能重复")
        return value

    @field_validator("deadline")
    @classmethod
    def utc_deadline(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


class ModuleInfo(ModuleContent):
    plan_version_id: UUID


class PlanContent(PlanSchema):
    research_question: RequiredText = Field(max_length=10000)
    final_deliverable: RequiredText = Field(max_length=2000)
    deadline: AwareDatetime | None = None
    argument: str = Field(default="", max_length=50000)
    terms: list[TermDefinition] = Field(default_factory=list, max_length=200)
    data_definitions: list[DataDefinition] = Field(default_factory=list, max_length=200)
    modules: list[ModuleContent] = Field(default_factory=list, max_length=200)

    @field_validator("deadline")
    @classmethod
    def utc_deadline(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


class PlanPublish(PlanSchema):
    change_note: RequiredText = Field(max_length=2000)
    content: PlanContent


class PlanVersionInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    version: int
    change_note: str
    content: PlanContent
    published_by: UUID
    published_at: datetime
