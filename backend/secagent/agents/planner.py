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
        self,
        task: TaskRead,
        parsed: ParsedTask,
        *,
        observations: dict | None = None,
        replan_round: int = 0,
    ) -> tuple[list[PlanStep], ModelResponse]:
        if replan_round < 0:
            raise ValueError("replan_round must be non-negative")
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
        elif parsed.scene.value == "web_analysis":
            params_by_tool = {
                "url_guard": {"url": task.target_url},
                "http_fetch": {"url": task.target_url},
                "header_check": {"response": "$http"},
                "form_extract": {"response": "$http"},
            }
        payload = {
            "goal": parsed.goal,
            "replan_round": replan_round,
            "observations": observations or {},
            "allowed_tools": list(allowed_tools),
            "params_by_tool": params_by_tool,
            "risk_by_tool": {
                name: self.registry.get(name).risk_level.value
                for name in allowed_tools
            },
        }
        if parsed.scene.value == "web_analysis":
            payload["web_planning_rules"] = [
                "Run http_fetch before header_check or form_extract.",
                "header_check must use exactly {'response': '$http'}.",
                "form_extract must use exactly {'response': '$http'}.",
                "Do not pass a URL string as the response parameter.",
                "Use only passive GET/HEAD observations unless explicitly authorized.",
            ]
        response = await self.router.complete(
            ModelStage.PLAN,
            ModelRequest(
                system=(
                    "Generate a minimal executable plan using only the provided "
                    "allowlisted tools and parameter templates."
                ),
                user=json.dumps(payload, ensure_ascii=False),
                response_schema=PlanDocument.model_json_schema(),
            ),
            preferred=(
                task.preferred_model if task.route_mode is RouteMode.MANUAL else None
            ),
        )
        steps = PlanDocument.model_validate(response.data).steps
        allowed = set(allowed_tools)
        if any(step.tool_name not in allowed for step in steps):
            raise ValueError("plan selected a tool outside the scene allowlist")
        return steps, response
