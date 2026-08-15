from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from secagent.config import Settings
from secagent.security.provider_credentials import (
    CredentialEncryptionConfigurationError,
    ProviderCredentialCipher,
    key_hint,
    normalize_api_key,
)


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
