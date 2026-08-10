from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "secagent-x"
    database_url: str = "sqlite:///./data/secagent.db"
    data_dir: Path = Path("data")
    model_mode: str = "auto"


@lru_cache
def get_settings() -> Settings:
    return Settings()
