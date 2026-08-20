from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from secagent.config import Settings
from secagent.db import Base, make_engine
from secagent.db_models import AuditEventRow, ProviderCredentialRow
from secagent.security.provider_credentials import (
    CredentialEncryptionConfigurationError,
    ProviderCredentialCipher,
    key_hint,
    normalize_api_key,
)
from secagent.services.provider_credentials import ProviderCredentialStore


def test_cipher_round_trip_never_stores_plaintext() -> None:
    settings = Settings(
        provider_credential_encryption_key=Fernet.generate_key().decode()
    )
    cipher = ProviderCredentialCipher.from_settings(settings)

    encrypted = cipher.encrypt("provider-secret-123")

    assert encrypted != "provider-secret-123"
    assert cipher.decrypt(encrypted) == "provider-secret-123"


def test_normalize_rejects_multiline_and_blank_keys() -> None:
    with pytest.raises(ValueError):
        normalize_api_key("  \n")
    with pytest.raises(ValueError):
        normalize_api_key("key\nwith-newline")


def test_normalize_rejects_overlong_keys() -> None:
    with pytest.raises(ValueError):
        normalize_api_key("x" * 513)


def test_key_hint_does_not_expose_short_key() -> None:
    assert key_hint("abcd") == "***"
    assert key_hint("provider-secret-1234") == "...1234"


def test_missing_or_invalid_master_key_is_configuration_error() -> None:
    with pytest.raises(CredentialEncryptionConfigurationError):
        ProviderCredentialCipher.from_settings(Settings())

    with pytest.raises(CredentialEncryptionConfigurationError):
        ProviderCredentialCipher.from_settings(
            Settings(provider_credential_encryption_key="not-a-fernet-key")
        )


def test_settings_accessor_prefers_master_key_file(tmp_path: Path) -> None:
    secret_file = tmp_path / "provider-encryption-key"
    secret_file.write_text("file-master-key\n", encoding="utf-8")
    settings = Settings(
        provider_credential_encryption_key="environment-master-key",
        provider_credential_encryption_key_file=secret_file,
    )

    assert settings.provider_credential_key() == "file-master-key"


def _credential_settings() -> Settings:
    return Settings(
        provider_credential_encryption_key=Fernet.generate_key().decode(),
        deepseek_api_key="environment-deepseek",
        glm_api_key="environment-glm",
    )


def test_store_encrypts_and_lists_only_safe_status(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'credentials.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        status = ProviderCredentialStore(session, _credential_settings()).save(
            "deepseek", "database-deepseek-1234", None
        )
        row = session.get(ProviderCredentialRow, "deepseek")
        statuses = ProviderCredentialStore(
            session, _credential_settings()
        ).list_status()

    assert status.configured is True
    assert status.key_hint == "...1234"
    assert row is not None
    assert row.encrypted_api_key != "database-deepseek-1234"
    assert "database-deepseek-1234" not in str(row.__dict__)
    assert {item.provider for item in statuses} == {"deepseek", "glm"}


def test_store_database_value_wins_and_clear_tombstone_disables_fallback(
    tmp_path: Path,
) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'precedence.db'}")
    Base.metadata.create_all(engine)
    settings = _credential_settings()

    with Session(engine) as session:
        store = ProviderCredentialStore(session, settings)
        store.save("deepseek", "database-deepseek-1234", None)
        assert store.resolve_keys(
            {"deepseek": "environment-deepseek", "glm": "environment-glm"}
        )["deepseek"] == "database-deepseek-1234"

        store.clear("deepseek", None)
        row = session.get(ProviderCredentialRow, "deepseek")
        resolved = store.resolve_keys(
            {"deepseek": "environment-deepseek", "glm": "environment-glm"}
        )

    assert row is not None
    assert row.state == "cleared"
    assert row.encrypted_api_key is None
    assert resolved["deepseek"] is None
    assert resolved["glm"] == "environment-glm"


def test_store_records_redacted_credential_audit_events(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        store = ProviderCredentialStore(session, _credential_settings())
        store.save("glm", "glm-database-5678", None)
        store.clear("glm", None)
        events = list(session.scalars(select(AuditEventRow)))

    assert [event.action for event in events] == [
        "provider.credentials.save",
        "provider.credentials.clear",
    ]
    assert "glm-database-5678" not in " ".join(event.details_json for event in events)


def test_store_rejects_unknown_provider(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'validation.db'}")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        store = ProviderCredentialStore(session, _credential_settings())
        with pytest.raises(ValueError, match="unsupported provider"):
            store.save("openai", "provider-key", None)
