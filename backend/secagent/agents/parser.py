import json

from secagent.domain import (
    ModelRequest,
    ModelResponse,
    ModelStage,
    ParsedTask,
    RouteMode,
    TaskRead,
)
from secagent.providers.router import ModelRouter


class TaskParser:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    async def parse(self, task: TaskRead) -> tuple[ParsedTask, ModelResponse]:
        payload = {
            "goal": task.goal,
            "authorization_scope": task.authorization_scope,
            "scene_hint": task.scene_hint.value if task.scene_hint else None,
            "target_url": task.target_url,
            "inputs": [],
        }
        response = await self.router.complete(
            ModelStage.TASK_PARSE,
            ModelRequest(
                system="将授权的网络安全任务解析为严格 JSON。",
                user=json.dumps(payload, ensure_ascii=False),
                response_schema=ParsedTask.model_json_schema(),
            ),
            preferred=(
                task.preferred_model if task.route_mode is RouteMode.MANUAL else None
            ),
        )
        return ParsedTask.model_validate(response.data), response
