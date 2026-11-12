from functools import lru_cache
from pathlib import Path
from uuid import UUID

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Team.s v2"
    database_url: str = "postgresql+psycopg://teams:teams_local_dev@127.0.0.1:5432/teams_dev"
    static_dir: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    review_job_timeout_seconds: float = Field(default=300.0, gt=0)
    review_job_lease_grace_seconds: float = Field(default=5.0, ge=0)
    review_job_max_attempts: int = Field(default=3, ge=1)
    review_job_retry_delay_seconds: float = Field(default=5.0, ge=0)
    review_return_limit: int = Field(default=3, ge=1)
    bottleneck_return_days: int = Field(default=3, ge=1)
    deepseek_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    llm_enable_live_calls: bool = False
    llm_project_daily_reviews: int = Field(default=20, ge=0)
    llm_platform_daily_reviews: int = Field(default=200, ge=0)
    llm_platform_daily_tokens: int = Field(default=1_000_000, ge=0)
    llm_max_calls_per_review: int = Field(default=64, ge=1)
    session_ttl_seconds: int = Field(default=14 * 24 * 3600, gt=0)
    session_cookie_secure: bool = False
    platform_admin_user_ids: str = ""
    storage_dir: Path = Path(__file__).resolve().parents[2] / ".storage"
    review_debug_dir: Path | None = None
    upload_max_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    mcp_allowed_hosts: str = "127.0.0.1:*,localhost:*,[::1]:*"
    mcp_allowed_origins: str = "http://127.0.0.1:*,http://localhost:*,http://[::1]:*"

    @field_validator("platform_admin_user_ids")
    @classmethod
    def validate_admin_ids(cls, value: str) -> str:
        return ",".join(str(UUID(item.strip())) for item in value.split(",") if item.strip())

    @property
    def admin_user_ids(self) -> frozenset[str]:
        return frozenset(self.platform_admin_user_ids.split(",")) - {""}

    @property
    def review_debug_path(self) -> Path:
        return self.review_debug_dir or self.storage_dir / "review-debug"


@lru_cache
def get_settings() -> Settings:
    return Settings()
