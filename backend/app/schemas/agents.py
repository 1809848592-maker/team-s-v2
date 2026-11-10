from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    client_type: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
    ]

    @field_validator("*", mode="before")
    @classmethod
    def reject_control_characters(cls, value):
        if isinstance(value, str) and any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("名称和客户端类型不能包含控制字符")
        return value


class AgentInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    owner_id: UUID
    name: str
    client_type: str
    created_at: datetime
    revoked_at: datetime | None


class AgentCreated(BaseModel):
    agent: AgentInfo
    token: str
