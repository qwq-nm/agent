from cryptography.fernet import Fernet, InvalidToken

SUPPORTED_PROVIDERS = ("deepseek", "glm")
MAX_API_KEY_LENGTH = 512


class CredentialEncryptionConfigurationError(RuntimeError):
    """The credential encryption key is missing, invalid, or unusable."""


def normalize_api_key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("api key must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("api key must not be blank")
    if "\r" in normalized or "\n" in normalized:
        raise ValueError("api key must be a single line")
    if len(normalized) > MAX_API_KEY_LENGTH:
        raise ValueError("api key is too long")
    return normalized


def key_hint(value: str) -> str:
    normalized = normalize_api_key(value)
    return "***" if len(normalized) <= 4 else f"...{normalized[-4:]}"


class ProviderCredentialCipher:
    def __init__(self, fernet: Fernet) -> None:
        self._fernet = fernet

    @classmethod
    def from_settings(cls, settings) -> "ProviderCredentialCipher":
        try:
            raw_key = settings.provider_credential_key()
            if not raw_key:
                raise ValueError("missing encryption key")
            return cls(Fernet(raw_key.encode("ascii")))
        except (OSError, TypeError, UnicodeEncodeError, ValueError) as exc:
            raise CredentialEncryptionConfigurationError(
                "Provider credential encryption is unavailable"
            ) from exc

    def encrypt(self, value: str) -> str:
        try:
            normalized = normalize_api_key(value)
            return self._fernet.encrypt(normalized.encode("utf-8")).decode("ascii")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ValueError("api key is invalid") from exc

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, TypeError, UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
            raise CredentialEncryptionConfigurationError(
                "Provider credential encryption is unavailable"
            ) from exc
