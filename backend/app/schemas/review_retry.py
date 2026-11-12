from pydantic import BaseModel, ConfigDict, Field


class ReviewRetryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    expected_revision: int = Field(strict=True, ge=1, le=2147483647)
