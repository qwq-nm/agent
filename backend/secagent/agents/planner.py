import json

from pydantic import BaseModel, Field

from secagent.agents.scenes import SCENES
from secagent.domain import (
    ModelRequest,
    ModelResponse,
    ModelStage,
    ParsedTask,
    PlanStep,
    RouteMode,
    TaskRead,
)
from secagent.providers.router import ModelRouter
from secagent.tools.registry import ToolRegistry


class PlanDocument(BaseModel):
    steps: list[PlanStep] = Field(default_factory=list)


class Planner:
    def __init__(self, router: ModelRouter, registry: ToolRegistry) -> None:
        self.router = router
        self.registry = registry

    async def plan(
        self, task: TaskRead, parsed: ParsedTask
    ) -> tuple[list[PlanStep], ModelResponse]:
        policy = SCENES[parsed.scene]
        payload = {
            "goal": parsed.goal,
            "allowed_tools": list(policy.allowed_tools),
            "params_by_tool": {name: {} for name in policy.allowed_tools},
            "risk_by_tool": {
                name: self.registry.get(name).risk_level.value
                for name in policy.allowed_tools
            },
        }
        response = await self.router.complete(
            ModelStage.PLAN,
            ModelRequest(
                system="只使用给定白名单工具生成最小可执行计划。",
                user=json.dumps(payload, ensure_ascii=False),
                response_schema=PlanDocument.model_json_schema(),
            ),
            preferred=(
                task.preferred_model if task.route_mode is RouteMode.MANUAL else None
            ),
        )
        return PlanDocument.model_validate(response.data).steps, response
