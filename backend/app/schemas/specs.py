from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

RequiredText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SpecSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*")
    @classmethod
    def reject_null_character(cls, value):
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("文字不能包含空字符")
        return value


class SpecItem(SpecSchema):
    number: RequiredText = Field(max_length=120)
    category: Literal["writing", "template", "format", "handoff"]
    requirement: RequiredText = Field(max_length=10000)
    check_method: Literal["program", "ai", "manual"]
    positive_example: str = Field(default="", max_length=10000)
    negative_example: str = Field(default="", max_length=10000)
    template_resource_id: UUID | None = None


class SpecPublish(SpecSchema):
    change_note: RequiredText = Field(max_length=2000)
    items: list[SpecItem] = Field(min_length=1, max_length=500)

    @field_validator("items")
    @classmethod
    def unique_numbers(cls, value: list[SpecItem]) -> list[SpecItem]:
        numbers = [item.number for item in value]
        if len(numbers) != len(set(numbers)):
            raise ValueError("规范条目的编号不能重复")
        return value


class SpecVersionInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    version: int
    change_note: str
    items: list[SpecItem]
    published_by: UUID
    published_at: datetime
