from fastapi import FastAPI

from secagent import db_models  # noqa: F401 -- registers SQLAlchemy tables
from secagent.api.tasks import router as tasks_router
from secagent.api.system import router as system_router
from secagent.config import Settings, get_settings
from secagent.db import make_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="SecAgent-X", version="0.1.0")
    app.state.settings = settings or get_settings()
    app.state.session_factory = make_session_factory(app.state.settings.database_url)
    app.include_router(system_router)
    app.include_router(tasks_router)
    return app


app = create_app()
