from fastapi import FastAPI

from secagent.api.system import router as system_router
from secagent.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="SecAgent-X", version="0.1.0")
    app.state.settings = settings or get_settings()
    app.include_router(system_router)
    return app


app = create_app()
