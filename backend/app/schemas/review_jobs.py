from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ReviewProgress(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: Literal["queued", "running", "retry_wait", "succeeded", "failed"]
    attempt: int
    max_attempts: int
    message: str
    last_error: (
        Literal[
            "pipeline_not_ready", "execution_error", "timeout", "lease_expired", "stale_workflow"
        ]
        | None
    )
    next_attempt_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
