from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Team.s v2"
    database_url: str = "postgresql+psycopg://teams:teams_local_dev@127.0.0.1:5432/teams_dev"
    static_dir: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    worker_poll_seconds: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
