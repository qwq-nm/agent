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


SAFETY_POLICY_GUIDANCE = {
    "conservative": {
        "label": "保守模式",
        "auto_execute": ["low"],
        "requires_approval": ["medium"],
        "must_not_plan": ["high", "forbidden"],
        "guidance": "优先规划低风险、被动、只读工具；避免主动验证、提交 payload、爆破、破坏性操作和任何越权访问。",
    },
    "standard": {
        "label": "标准模式",
        "auto_execute": ["low"],
        "requires_approval": ["medium"],
        "must_not_plan": ["high", "forbidden"],
        "guidance": "可以规划必要的中风险验证类动作，但必须给出明确目的；高风险和禁止动作不要规划。",
    },
    "expert": {
        "label": "专家模式",
        "auto_execute": ["low"],
        "requires_approval": ["medium", "high"],
        "must_not_plan": ["forbidden"],
        "guidance": "可以规划中高风险授权验证动作，但必须最小化步骤、说明授权依据，并依赖人工确认；禁止动作永远不能规划。",
    },
}


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
        execution_memory: dict | None = None,
        next_focus: list[str] | None = None,
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
                "link_extract": {"response": "$http"},
                "robots_analyzer": {
                    "base_url": task.target_url,
                    "response": "$http",
                },
                "js_analyzer": {"response": "$http"},
                "path_normalizer": {
                    "base_url": task.target_url,
                    "response": "$http",
                },
                "flag_pattern_detector": {"response": "$http"},
                "cookie_analyzer": {"response": "$http"},
                "sensitive_file_checker": {"response": "$http"},
            }

        payload = {
            "goal": parsed.goal,
            "replan_round": replan_round,
            "safety_mode": task.safety_mode.value,
            "safety_policy": SAFETY_POLICY_GUIDANCE[task.safety_mode.value],
            "observations": observations or {},
            "next_focus": next_focus or [],
            "execution_memory": execution_memory or {},
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
                "header_check and form_extract must use exactly {'response': '$http'}.",
                "link_extract, js_analyzer, path_normalizer, and flag_pattern_detector should analyze the latest {'response': '$http'}.",
                "robots_analyzer can suggest or parse robots.txt from {'base_url': target_url, 'response': '$http'}.",
                "Do not pass a URL string as the response parameter.",
                "Use only passive GET/HEAD observations unless explicitly authorized.",
                "Prefer public discovery steps: links, forms, robots.txt hints, JavaScript route hints, then flag-like pattern detection.",
                "cookie_analyzer and sensitive_file_checker are low-risk passive tools that should analyze the latest {'response': '$http'}.",
                "Do not repeat tool calls listed in execution_memory.successful_tool_calls unless the observations show the earlier call failed or became stale.",
                "Use next_focus to choose the smallest set of new evidence-gathering steps.",
                "When execution_memory.latest_evidence contains public links, forms, robots hints, JavaScript hints, or candidate paths, plan the next smallest passive step that investigates those new observations instead of repeating the homepage.",
                "Each step purpose must mention which missing evidence or next_focus item it addresses.",
            ]

        response = await self.router.complete(
            ModelStage.PLAN,
            ModelRequest(
                system=(
                    "生成最小可执行安全分析计划，只能使用 payload.allowed_tools 中的白名单工具和 params_by_tool 中给出的参数模板。"
                    "必须遵守 payload.safety_policy：auto_execute 风险等级可自动执行，requires_approval 风险等级可以规划但需要人工确认，"
                    "must_not_plan 风险等级不得规划。"
                    "如果这是重规划，请优先阅读 observations、next_focus 和 execution_memory.latest_evidence，"
                    "根据已有工具结果补充缺失证据，避免重复执行 successful_tool_calls 中已经成功且未过期的同参数工具。"
                    "每一步 purpose 必须说明：为什么需要该工具、它补充哪类证据、它如何服务于用户目标。"
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
