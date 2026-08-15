from collections.abc import Mapping

from sqlalchemy.orm import Session

from secagent.config import Settings
from secagent.providers import build_providers
from secagent.providers.router import ModelRouter
from secagent.services.provider_credentials import ProviderCredentialStore


class ProviderRuntimeFactory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build(self, session: Session) -> ModelRouter:
        provider_keys: Mapping[str, str | None] = ProviderCredentialStore(
            session, self.settings
        ).resolve_keys(
            {
                "deepseek": self.settings.deepseek_key(),
                "glm": self.settings.glm_key(),
            }
        )
        providers = build_providers(
            self.settings,
            provider_keys=provider_keys,
            allow_missing_live=True,
        )
        return ModelRouter(
            providers,
            mode=self.settings.model_mode,
            allow_missing=self.settings.model_mode == "live",
        )
