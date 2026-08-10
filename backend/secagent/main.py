from fastapi import FastAPI

from secagent import db_models  # noqa: F401 -- registers SQLAlchemy tables
from secagent.api.tasks import router as tasks_router
from secagent.api.system import router as system_router
from secagent.config import Settings, get_settings
from secagent.db import make_session_factory
from secagent.providers import build_providers
from secagent.providers.router import ModelRouter
from secagent.tools.registry import ToolRegistry


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="SecAgent-X", version="0.1.0")
    app.state.settings = settings or get_settings()
    app.state.session_factory = make_session_factory(app.state.settings.database_url)
    app.state.model_router = ModelRouter(
        build_providers(app.state.settings), mode=app.state.settings.model_mode
    )
    app.state.tool_registry = ToolRegistry([])
    app.include_router(system_router)
    app.include_router(tasks_router)
    return app


app = create_app()
