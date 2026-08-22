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
                "在执行 header_check、form_extract、link_extract、js_analyzer、path_normalizer、flag_pattern_detector、cookie_analyzer 或 sensitive_file_checker 前，必须先通过 http_fetch 获得结构化 HTTP 响应。",
                "header_check 和 form_extract 的参数必须严格使用 {'response': '$http'}。",
                "link_extract、js_analyzer、path_normalizer、flag_pattern_detector、cookie_analyzer 和 sensitive_file_checker 应分析最新的 {'response': '$http'}。",
                "robots_analyzer 可以使用 {'base_url': target_url, 'response': '$http'} 生成或解析 robots.txt 线索。",
                "不要把 URL 字符串传给 response 参数；response 只能来自 http_fetch 的结构化结果。",
                "除非用户明确授权，否则只规划被动 GET/HEAD 观察，不提交表单、不爆破、不执行真实漏洞利用。",
                "优先规划公开发现步骤：链接、表单、robots.txt、前端脚本路由、候选路径、疑似 Flag 模式。",
                "不要重复 execution_memory.successful_tool_calls 中已经成功且参数相同的工具，除非观察结果表明之前失败、过期或证据不足。",
                "使用 next_focus 选择最小必要的补充证据步骤。",
                "如果 execution_memory.latest_evidence 已包含公开链接、表单、robots 线索、JS 线索或候选路径，应规划调查这些新线索的最小被动步骤，而不是重复抓取首页。",
                "每一步 purpose 必须使用正式中文表述，说明执行依据、补充的缺失证据，以及该证据如何影响下一步模型判断。",
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
                    "所有 step.name 必须是简短中文动作标题，例如“校验目标 URL 是否安全”“获取首页内容”“分析响应头”。"
                    "所有 step.purpose 必须使用正式中文，说明：执行依据、补充哪类证据、工具结果如何影响下一步模型判断。"
                    "如果某一步依赖前置工具输出，purpose 必须说明前置依赖；如果是重试或重规划，必须说明上一轮失败原因或证据缺口。"
                    "除工具名、URL、路径、HTTP 字段、代码片段等技术原文外，不要输出英文解释。"
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
