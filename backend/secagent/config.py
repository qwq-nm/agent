from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "secagent-x"
    database_url: str = "sqlite:///./data/secagent.db"
    data_dir: Path = Path("data")
    model_mode: str = "auto"
    model_timeout_seconds: float = 30.0
    upload_max_bytes: int = 10 * 1024 * 1024
    archive_max_files: int = 200
    archive_max_bytes: int = 50 * 1024 * 1024
    web_allowed_hosts: str = "web-demo"

    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"

    glm_api_key: str | None = None
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_model: str = "glm-4-flash"


@lru_cache
def get_settings() -> Settings:
    return Settings()
