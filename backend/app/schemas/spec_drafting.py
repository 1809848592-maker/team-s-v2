from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.schemas.specs import SpecItem


class SpecDraftGenerate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)
    ]
    disclosure_version: str = Field(min_length=1, max_length=80)

    @field_validator("description", "disclosure_version")
    @classmethod
    def valid_text(cls, value):
        if any(ord(char) < 32 and char not in "\n\r\t" or ord(char) == 127 for char in value):
            raise ValueError("文字不能包含控制字符")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("请使用有效的文字") from None
        return value


class SpecDraftGenerated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    call_id: UUID
    instruction_version: str
    items: list[SpecItem]
