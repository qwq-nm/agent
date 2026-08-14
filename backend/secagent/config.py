from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from secagent.config_secrets import read_secret


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "secagent-x"
    database_url: str = "postgresql+psycopg://secagent:secagent@postgres/secagent"
    redis_url: str = "redis://redis:6379/0"
    worker_concurrency: int = 3
    job_lease_seconds: int = 90
    job_heartbeat_seconds: int = 15
    job_auto_retries: int = 1
    jwt_access_minutes: int = 15
    jwt_refresh_days: int = 7
    cookie_secure: bool = False
    jwt_signing_key: str | None = None
    jwt_signing_key_file: Path | None = None
    data_dir: Path = Path("data")
    model_mode: str = "auto"
    model_timeout_seconds: float = 30.0
    upload_max_bytes: int = 10 * 1024 * 1024
    archive_max_files: int = 200
    archive_max_bytes: int = 50 * 1024 * 1024
    web_allowed_hosts: str = "web-demo"
    max_steps_per_task: int = 20
    max_replans: int = 2
    task_timeout_seconds: int = 300

    deepseek_api_key: str | None = None
    deepseek_api_key_file: Path | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"

    glm_api_key: str | None = None
    glm_api_key_file: Path | None = None
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_model: str = "glm-4-flash"

    max_model_calls_per_task: int = 8
    max_input_tokens_per_task: int = 120_000
    max_output_tokens_per_task: int = 24_000

    def deepseek_key(self) -> str | None:
        return read_secret(self.deepseek_api_key, self.deepseek_api_key_file)

    def glm_key(self) -> str | None:
        return read_secret(self.glm_api_key, self.glm_api_key_file)

    def jwt_key(self) -> str | None:
        return read_secret(self.jwt_signing_key, self.jwt_signing_key_file)


@lru_cache
def get_settings() -> Settings:
    return Settings()
