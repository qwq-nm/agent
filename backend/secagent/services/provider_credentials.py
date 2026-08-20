from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from secagent.config import Settings
from secagent.db_models import ProviderCredentialRow
from secagent.security.provider_credentials import (
    SUPPORTED_PROVIDERS,
    CredentialEncryptionConfigurationError,
    ProviderCredentialCipher,
    key_hint,
    normalize_api_key,
)
from secagent.services.audit import AuditService


@dataclass(frozen=True)
class ProviderCredentialStatus:
    provider: str
    configured: bool
    key_hint: str | None
    updated_at: datetime | None


class ProviderCredentialStore:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def list_status(self) -> list[ProviderCredentialStatus]:
        rows = {
            row.provider: row
            for row in self.session.scalars(
                select(ProviderCredentialRow).where(
                    ProviderCredentialRow.provider.in_(SUPPORTED_PROVIDERS)
                )
            ).all()
        }
        return [self._status(provider, rows.get(provider)) for provider in SUPPORTED_PROVIDERS]

    def resolve_keys(
        self, fallbacks: Mapping[str, str | None]
    ) -> dict[str, str | None]:
        rows = {
            row.provider: row
            for row in self.session.scalars(
                select(ProviderCredentialRow).where(
                    ProviderCredentialRow.provider.in_(SUPPORTED_PROVIDERS)
                )
            ).all()
        }
        resolved: dict[str, str | None] = {}
        for provider in SUPPORTED_PROVIDERS:
            row = rows.get(provider)
            if row is None:
                resolved[provider] = fallbacks.get(provider)
            elif row.state == "cleared":
                resolved[provider] = None
            elif row.state == "configured" and row.encrypted_api_key:
                resolved[provider] = self._cipher().decrypt(row.encrypted_api_key)
            else:
                raise CredentialEncryptionConfigurationError(
                    "Provider credential encryption is unavailable"
                )
        return resolved

    def save(
        self, provider: str, api_key: str, actor_id: str | None
    ) -> ProviderCredentialStatus:
        self._require_provider(provider)
        normalized = normalize_api_key(api_key)
        encrypted = self._cipher().encrypt(normalized)
        row = self.session.get(ProviderCredentialRow, provider)
        if row is None:
            row = ProviderCredentialRow(provider=provider)
            self.session.add(row)
        row.encrypted_api_key = encrypted
        row.key_hint = key_hint(normalized)
        row.state = "configured"
        row.updated_by = actor_id
        self._commit_audit(actor_id, "provider.credentials.save", provider)
        return self._status(provider, row)

    def clear(
        self, provider: str, actor_id: str | None
    ) -> ProviderCredentialStatus:
        self._require_provider(provider)
        row = self.session.get(ProviderCredentialRow, provider)
        if row is None:
            row = ProviderCredentialRow(provider=provider)
            self.session.add(row)
        row.encrypted_api_key = None
        row.key_hint = None
        row.state = "cleared"
        row.updated_by = actor_id
        self._commit_audit(actor_id, "provider.credentials.clear", provider)
        return self._status(provider, row)

    def _cipher(self) -> ProviderCredentialCipher:
        return ProviderCredentialCipher.from_settings(self.settings)

    def _commit_audit(self, actor_id: str | None, action: str, provider: str) -> None:
        try:
            AuditService(self.session).record(
                actor_id,
                action,
                "provider",
                provider,
                "success",
                {"provider": provider},
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

    @staticmethod
    def _status(
        provider: str, row: ProviderCredentialRow | None
    ) -> ProviderCredentialStatus:
        return ProviderCredentialStatus(
            provider=provider,
            configured=row is not None and row.state == "configured" and row.encrypted_api_key is not None,
            key_hint=row.key_hint if row is not None and row.state == "configured" else None,
            updated_at=row.updated_at if row is not None else None,
        )

    @staticmethod
    def _require_provider(provider: str) -> None:
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"unsupported provider: {provider}")
