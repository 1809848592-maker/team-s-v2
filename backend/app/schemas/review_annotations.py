"""人的逐条意见处理请求；身份及审核来源只能由服务端确定。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReviewAnnotationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    handling: Literal["adopted", "not_adopted"]
    reason: str = Field(default="", max_length=4000)
    expected_revision: int = Field(strict=True, ge=0)

    @field_validator("reason")
    @classmethod
    def reject_null_character(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("理由不能包含空字符")
        return value

    @model_validator(mode="after")
    def rejected_reason(self):
        if self.handling == "not_adopted" and not self.reason.strip():
            raise ValueError("不采纳审核意见时必须填写理由")
        return self
