from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.deliveries import AIParticipation


class AgentDraftUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handoff_note: str = Field(default="", max_length=10000)

    @field_validator("handoff_note")
    @classmethod
    def reject_null_character(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("交接说明不能包含空字符")
        return value


class AgentDraftInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    module_id: UUID
    owner_id: UUID
    agent_id: UUID
    filename: str
    sha256: str
    size: int
    handoff_note: str
    created_at: datetime
    status: Literal["pending_confirmation"] = "pending_confirmation"


class AgentDraftView(AgentDraftInfo):
    status: Literal["pending_confirmation", "confirmed"]
    agent_name: str
    download_url: str
    delivery_id: UUID | None
    confirmed_at: datetime | None


class DraftConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ai_participation: AIParticipation
    ai_note: str = Field(default="", max_length=2000)

    @field_validator("ai_note")
    @classmethod
    def reject_null_character(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("说明不能包含空字符")
        return value
