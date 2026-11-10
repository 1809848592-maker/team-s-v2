from functools import lru_cache
from pathlib import Path
from uuid import UUID

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Team.s v2"
    database_url: str = "postgresql+psycopg://teams:teams_local_dev@127.0.0.1:5432/teams_dev"
    static_dir: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    worker_poll_seconds: float = 30.0
    session_ttl_seconds: int = Field(default=14 * 24 * 3600, gt=0)
    session_cookie_secure: bool = False
    platform_admin_user_ids: str = ""

    @field_validator("platform_admin_user_ids")
    @classmethod
    def validate_admin_ids(cls, value: str) -> str:
        return ",".join(str(UUID(item.strip())) for item in value.split(",") if item.strip())

    @property
    def admin_user_ids(self) -> frozenset[str]:
        return frozenset(self.platform_admin_user_ids.split(",")) - {""}


@lru_cache
def get_settings() -> Settings:
    return Settings()
