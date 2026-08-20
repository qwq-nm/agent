from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from secagent.config import Settings
from secagent.db_models import ProviderRouteRow
from secagent.services.audit import AuditService


DEEPSEEK_ROUTE_PRESETS = {
    "opencode-go": {
        "provider": "deepseek",
        "route": "opencode-go",
        "display_name": "OpenCode Go",
        "base_url": "https://opencode.ai/zen/go/v1",
        "model": "deepseek-v4-flash",
        "api_style": "opencode-go",
        "reasoning_effort": "high",
    },
    "deepseek": {
        "provider": "deepseek",
        "route": "deepseek",
        "display_name": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_style": "deepseek",
        "reasoning_effort": "high",
    },
}


@dataclass(frozen=True)
class ProviderRouteStatus:
    provider: str
    route: str
    display_name: str
    base_url: str
    model: str
    api_style: str
    reasoning_effort: str | None
    configured: bool
    updated_at: datetime | None


class ProviderRouteStore:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def deepseek_status(self) -> ProviderRouteStatus:
        row = self.session.get(ProviderRouteRow, "deepseek")
        if row is None:
            return self._status_from_settings(configured=False)
        return self._status_from_row(row)

    def deepseek_config(self) -> dict[str, str | None]:
        status = self.deepseek_status()
        return {
            "base_url": status.base_url,
            "model": status.model,
            "api_style": status.api_style,
            "reasoning_effort": status.reasoning_effort,
        }

    def save_deepseek_route(
        self, route: str, actor_id: str | None
    ) -> ProviderRouteStatus:
        if route not in DEEPSEEK_ROUTE_PRESETS:
            raise ValueError(f"unsupported deepseek route: {route}")
        preset = DEEPSEEK_ROUTE_PRESETS[route]
        row = self.session.get(ProviderRouteRow, "deepseek")
        if row is None:
            row = ProviderRouteRow(provider="deepseek")
            self.session.add(row)
        row.base_url = str(preset["base_url"])
        row.model = str(preset["model"])
        row.api_style = str(preset["api_style"])
        row.reasoning_effort = str(preset["reasoning_effort"])
        row.updated_by = actor_id
        try:
            AuditService(self.session).record(
                actor_id,
                "provider.route.save",
                "provider",
                "deepseek",
                "success",
                {"provider": "deepseek", "route": route},
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return self._status_from_row(row)

    def _status_from_settings(self, *, configured: bool) -> ProviderRouteStatus:
        style = self.settings.deepseek_api_style
        if style == "auto":
            style = (
                "opencode-go"
                if "opencode.ai" in self.settings.deepseek_base_url
                else "deepseek"
            )
        route = "opencode-go" if style == "opencode-go" else "deepseek"
        preset = DEEPSEEK_ROUTE_PRESETS[route]
        return ProviderRouteStatus(
            provider="deepseek",
            route=route,
            display_name=str(preset["display_name"]),
            base_url=self.settings.deepseek_base_url,
            model=self.settings.deepseek_model,
            api_style=style,
            reasoning_effort=self.settings.deepseek_reasoning_effort,
            configured=configured,
            updated_at=None,
        )

    @staticmethod
    def _status_from_row(row: ProviderRouteRow) -> ProviderRouteStatus:
        route = "opencode-go" if row.api_style == "opencode-go" else "deepseek"
        preset = DEEPSEEK_ROUTE_PRESETS[route]
        return ProviderRouteStatus(
            provider=row.provider,
            route=route,
            display_name=str(preset["display_name"]),
            base_url=row.base_url,
            model=row.model,
            api_style=row.api_style,
            reasoning_effort=row.reasoning_effort,
            configured=True,
            updated_at=row.updated_at,
        )
