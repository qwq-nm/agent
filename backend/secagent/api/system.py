from fastapi import APIRouter, Request

router = APIRouter(prefix="/api")


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "secagent-x"}


@router.get("/models/status")
def model_status(request: Request) -> list[dict]:
    return request.app.state.model_router.describe()


@router.get("/tools")
def tool_status(request: Request) -> list[dict]:
    return request.app.state.tool_registry.describe()
