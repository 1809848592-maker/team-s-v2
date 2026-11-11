"""负责人终审请求；状态、操作者和自审标记均由服务端确定。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SpecificationRuling(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    finding_id: UUID
    decision: Literal["accept", "reject"]
    comment: str = Field(default="", max_length=4000)

    @field_validator("comment")
    @classmethod
    def reject_null_character(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("终审意见不能包含空字符")
        return value

    @model_validator(mode="after")
    def rejected_comment(self):
        if self.decision == "reject" and not self.comment.strip():
            raise ValueError("驳回规范不适用时必须附上意见")
        return self


class FinalReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    decision: Literal["approve", "return"]
    comment: str = Field(default="", max_length=4000)
    expected_revision: int = Field(strict=True, ge=1)
    context_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    spec_rulings: list[SpecificationRuling] = Field(default_factory=list, max_length=5000)

    @field_validator("comment")
    @classmethod
    def reject_null_character(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("终审意见不能包含空字符")
        return value

    @model_validator(mode="after")
    def validate_return_and_ruling_ids(self):
        if self.decision == "return" and not self.comment.strip():
            raise ValueError("终审退回时必须附上意见")
        identities = [ruling.finding_id for ruling in self.spec_rulings]
        if len(identities) != len(set(identities)):
            raise ValueError("同一规范意见不能重复裁定")
        return self
