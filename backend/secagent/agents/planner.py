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
    TaskScene,
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
        "guidance": "优先规划低风险、被动、只读工具；中风险工具必须人工确认；高风险、破坏性、大规模爆破和任何越权访问不得规划。",
    },
    "standard": {
        "label": "标准模式",
        "auto_execute": ["low"],
        "requires_approval": ["medium"],
        "must_not_plan": ["high", "forbidden"],
        "guidance": "可以规划必要的中风险授权验证动作，但必须给出明确目的并等待人工确认；高风险、破坏性、大规模爆破和禁止动作不得规划。",
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
        elif parsed.scene in {TaskScene.WEB_ANALYSIS, TaskScene.CTF_WEB}:
            params_by_tool = {
                "url_guard": {"url": task.target_url},
                "http_fetch": {"url": task.target_url},
                "header_check": {"response": "$http"},
                "form_extract": {"response": "$http"},
                "link_extract": {"response": "$http"},
                "browser_snapshot": {"url": task.target_url, "wait_ms": 2500},
                "dirsearch_scan": {
                    "url": task.target_url,
                    "extensions": ["php", "html", "js", "txt", "json", "bak", "zip"],
                },
                "login_probe": {
                    "url": task.target_url,
                    "max_attempts": 20,
                },
                "sqlmap_probe": {
                    "url": task.target_url,
                    "level": 1,
                    "risk": 1,
                    "extra_args": [],
                },
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
        if parsed.scene in {TaskScene.WEB_ANALYSIS, TaskScene.CTF_WEB}:
            payload["web_planning_rules"] = [
                "在执行 header_check、form_extract、link_extract、js_analyzer、path_normalizer、flag_pattern_detector、cookie_analyzer 或 sensitive_file_checker 前，必须先通过 http_fetch 获得结构化 HTTP 响应。",
                "header_check 和 form_extract 的参数必须严格使用 {'response': '$http'}。",
                "link_extract、js_analyzer、path_normalizer、flag_pattern_detector、cookie_analyzer 和 sensitive_file_checker 应分析最新的 {'response': '$http'}。",
                "robots_analyzer 可以使用 {'base_url': target_url, 'response': '$http'} 生成或解析 robots.txt 线索。",
                "如果 http_fetch 获取到的 HTML 内容很少、只包含前端挂载节点、出现大量 script/app/root 字样，或用户明确提到 JS 渲染，应规划 browser_snapshot 获取浏览器渲染后的可见文本、链接、表单和截图。",
                "不要把 URL 字符串传给 response 参数；response 只能来自 http_fetch 的结构化结果。",
                "低风险工具可以自动执行；目录发现、浏览器渲染、登录探测、SQL 注入探测等主动验证工具必须在授权范围内规划，并按风险等级等待人工确认；禁止未授权爆破、大规模爆破、破坏性操作和越权访问。",
                "login_probe 会提交登录表单，属于高风险授权验证工具；只有当安全策略允许规划 high 风险动作、目标明确授权且已经发现登录页或 password 表单时，才可以规划，并且必须说明人工确认依据。",
                "sqlmap_probe 调用原版 sqlmap，属于高风险授权验证工具；只有当安全策略允许规划 high 风险动作、目标明确授权且已有 URL 参数或表单参数线索时，才可以规划，并且必须说明人工确认依据。",
                "优先规划公开发现步骤：链接、表单、robots.txt、前端脚本路由、候选路径、疑似 Flag 模式。",
                "不要重复 execution_memory.successful_tool_calls 中已经成功且参数相同的工具，除非观察结果表明之前失败、过期或证据不足。",
                "使用 next_focus 选择最小必要的补充证据步骤。",
                "如果 execution_memory.latest_evidence 已包含公开链接、表单、robots 线索、JS 线索或候选路径，应规划调查这些新线索的最小被动步骤，而不是重复抓取首页。",
                "每一步 purpose 必须使用正式中文表述，说明执行依据、补充的缺失证据，以及该证据如何影响下一步模型判断。",
            ]
        if parsed.scene is TaskScene.CTF_WEB:
            payload["ctf_web_rules"] = [
                "允许规划 dirsearch_scan 进行授权范围内的标准 dirsearch 路径发现；该工具属于中风险，必须经过人工确认，不得用于未授权目标。",
                "如果页面由 JavaScript 渲染、普通 HTTP 响应看不到题目内容或 flag 线索，应规划 browser_snapshot；该工具属于中风险，必须经过人工确认。",
                "如已发现登录表单、登录路径、后台入口或题目明显指向弱口令/万能密码方向，可在专家模式下规划 login_probe；它只做小规模常规弱口令和万能密码探测，必须等待人工确认，不得自动执行。",
                "如已发现查询参数、搜索入口、登录/查询表单或题目明确指向 SQL 注入方向，可在专家模式下规划 sqlmap_probe；该工具必须等待人工确认，不得自动执行。",
                "这是授权 CTF Web/靶场题目分析场景，目标是围绕公开页面和授权路径寻找题目线索、候选 flag 或下一步分析方向。",
                "优先分析首页、robots.txt、公开链接、前端 JS 路由、注释、表单字段、Cookie、响应头、备份文件名、配置文件名、源码泄露线索和页面中的 flag-like pattern。",
                "如果运行记忆中出现 queued_urls、sensitive_paths、robots_paths、js_files、api_endpoints 或 candidate_flags，应优先规划最小必要工具去验证这些线索。",
                "允许的自动动作仍然限于低风险白名单工具和授权范围；目录发现、登录探测、SQL 注入探测等主动验证动作必须等待人工确认并写入审计记录；禁止未授权目录爆破、大规模爆破、命令执行、文件写入、破坏性利用和越权访问。",
                "如果发现疑似 flag，先使用 flag_pattern_detector 或已有证据复核，不要伪造 flag；报告必须说明 flag 来源证据。",
                "如果没有发现 flag，应明确输出已检查的公开入口、剩余可能方向和需要新增工具能力的原因。",
            ]

        response = await self.router.complete(
            ModelStage.PLAN,
            ModelRequest(
                system=(
                    "生成最小可执行安全分析计划，只能使用 payload.allowed_tools 中的白名单工具和 params_by_tool 中给出的参数模板。"
                    "必须遵守 payload.safety_policy：auto_execute 风险等级可自动执行，requires_approval 风险等级可以规划但需要人工确认，"
                    "must_not_plan 风险等级不得规划。"
                    "如果 payload.ctf_web_rules 存在，说明这是授权 CTF Web 场景，必须优先遵守其中的线索发现、flag 复核和禁止动作要求。"
                    "如果这是重规划，请优先阅读 observations、next_focus、execution_memory.latest_evidence 和 execution_memory.runtime_memory，"
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
