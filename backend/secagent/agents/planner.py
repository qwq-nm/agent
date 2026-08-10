import json
from pathlib import Path

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
    def __init__(
        self, router: ModelRouter, registry: ToolRegistry, data_dir: Path
    ) -> None:
        self.router = router
        self.registry = registry
        self.data_dir = data_dir

    async def plan(
        self, task: TaskRead, parsed: ParsedTask
    ) -> tuple[list[PlanStep], ModelResponse]:
        policy = SCENES[parsed.scene]
        allowed_tools = policy.allowed_tools
        params_by_tool = {name: {} for name in allowed_tools}
        upload_metadata = self.data_dir / "tasks" / task.id / "upload.json"
        if parsed.scene.value == "incident_response":
            if upload_metadata.is_file():
                original_name = json.loads(
                    upload_metadata.read_text(encoding="utf-8")
                )["original_name"]
                params_by_tool = {
                    name: {
                        "file_path": "$upload",
                        "source_name": original_name,
                    }
                    for name in allowed_tools
                }
            else:
                allowed_tools = ("demo_evidence",)
                params_by_tool = {"demo_evidence": {}}
        elif parsed.scene.value == "source_audit":
            params_by_tool = {
                name: {"project_path": "$project"} for name in allowed_tools
            }
        payload = {
            "goal": parsed.goal,
            "allowed_tools": list(allowed_tools),
            "params_by_tool": params_by_tool,
            "risk_by_tool": {
                name: self.registry.get(name).risk_level.value
                for name in allowed_tools
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
