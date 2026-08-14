from pathlib import Path

import pytest

from secagent.config import Settings
from secagent.config_secrets import read_secret


def test_secret_file_takes_precedence(tmp_path: Path):
    secret_file = tmp_path / "key"
    secret_file.write_text("file-secret\n", encoding="utf-8")
    assert read_secret("env-secret", secret_file) == "file-secret"


def test_missing_secret_file_is_an_error(tmp_path: Path):
    with pytest.raises(ValueError, match="secret file does not exist"):
        read_secret(None, tmp_path / "missing")


def test_blank_secret_is_treated_as_unconfigured():
    assert read_secret("   ", None) is None


def test_deepseek_key_prefers_configured_secret_file(tmp_path: Path):
    secret_file = tmp_path / "deepseek-key"
    secret_file.write_text("deepseek-file-secret\n", encoding="utf-8")
    settings = Settings(
        deepseek_api_key="deepseek-environment-secret",
        deepseek_api_key_file=secret_file,
    )

    assert settings.deepseek_key() == "deepseek-file-secret"


def test_glm_key_prefers_configured_secret_file(tmp_path: Path):
    secret_file = tmp_path / "glm-key"
    secret_file.write_text("glm-file-secret\n", encoding="utf-8")
    settings = Settings(
        glm_api_key="glm-environment-secret",
        glm_api_key_file=secret_file,
    )

    assert settings.glm_key() == "glm-file-secret"


def test_jwt_key_prefers_configured_secret_file(tmp_path: Path):
    secret_file = tmp_path / "jwt-key"
    secret_file.write_text("jwt-file-secret\n", encoding="utf-8")
    settings = Settings(
        jwt_signing_key="jwt-environment-secret",
        jwt_signing_key_file=secret_file,
    )

    assert settings.jwt_key() == "jwt-file-secret"


def test_settings_secret_accessors_treat_blank_values_as_unconfigured():
    settings = Settings(
        deepseek_api_key="  ",
        glm_api_key="\t",
        jwt_signing_key="\n",
    )

    assert settings.deepseek_key() is None
    assert settings.glm_key() is None
    assert settings.jwt_key() is None


def test_settings_uses_required_production_defaults(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "DATABASE_URL",
        "REDIS_URL",
        "WORKER_CONCURRENCY",
        "JOB_LEASE_SECONDS",
        "JOB_HEARTBEAT_SECONDS",
        "JOB_AUTO_RETRIES",
        "JWT_ACCESS_MINUTES",
        "JWT_REFRESH_DAYS",
        "COOKIE_SECURE",
        "DEEPSEEK_MODEL",
        "GLM_MODEL",
        "MAX_MODEL_CALLS_PER_TASK",
        "MAX_INPUT_TOKENS_PER_TASK",
        "MAX_OUTPUT_TOKENS_PER_TASK",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.database_url == "postgresql+psycopg://secagent:secagent@postgres/secagent"
    assert settings.redis_url == "redis://redis:6379/0"
    assert settings.worker_concurrency == 3
    assert settings.job_lease_seconds == 90
    assert settings.job_heartbeat_seconds == 15
    assert settings.job_auto_retries == 1
    assert settings.jwt_access_minutes == 15
    assert settings.jwt_refresh_days == 7
    assert settings.cookie_secure is False
    assert settings.deepseek_model == "deepseek-v4-pro"
    assert settings.glm_model == "glm-5.2"
    assert settings.max_model_calls_per_task == 8
    assert settings.max_input_tokens_per_task == 120_000
    assert settings.max_output_tokens_per_task == 24_000
