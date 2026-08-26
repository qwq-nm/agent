import json

from pydantic import BaseModel, Field

from secagent.domain import ModelRequest, ModelResponse, ModelStage, ParsedTask
from secagent.providers.router import ModelRouter
from secagent.services.ledger import LedgerService


EVIDENCE_TYPE_LABELS = {
    "http_response": "HTTP 响应",
    "http_headers": "响应头",
    "html_form": "页面表单",
    "link_inventory": "链接清单",
    "robots_rules": "robots.txt 规则",
    "js_hint": "前端脚本线索",
    "normalized_path": "规范化路径",
    "flag_candidate": "疑似 Flag",
    "raw_line": "原始日志行",
    "log_summary": "日志摘要",
    "attack_pattern": "攻击模式",
    "source_file": "源码文件",
    "secret_candidate": "疑似敏感信息",
    "runtime_error": "运行错误",
    "observation": "观察结果",
    "http_observation": "HTTP 观察结果",
}

TOOL_LABELS = {
    "demo_evidence": "演示证据生成",
    "log_type_detector": "日志类型识别",
    "log_analyzer": "日志字段分析",
    "attack_pattern_detector": "攻击模式检测",
    "timeline_builder": "事件时间线构建",
    "project_detector": "项目类型识别",
    "source_scanner": "源码风险扫描",
    "secret_scanner": "敏感信息扫描",
    "config_checker": "配置风险检查",
    "url_guard": "URL 安全边界检查",
    "http_fetch": "HTTP 页面获取",
    "header_check": "响应头安全检查",
    "form_extract": "表单提取",
    "link_extract": "链接提取",
    "browser_snapshot": "浏览器渲染快照",
    "dirsearch_scan": "dirsearch 路径发现",
    "robots_analyzer": "robots.txt 分析",
    "js_analyzer": "前端脚本分析",
    "path_normalizer": "路径规范化",
    "flag_pattern_detector": "Flag 模式识别",
    "cookie_analyzer": "Cookie 安全属性分析",
    "sensitive_file_checker": "敏感文件线索检查",
    "report_generator": "报告生成",
}

TOOL_PURPOSES = {
    "url_guard": "检查目标 URL 是否位于授权范围内，并拦截 SSRF、文件协议、内网地址等越界访问。",
    "http_fetch": "访问授权页面，获取状态码、响应头和页面内容，为后续解析提供原始材料。",
    "header_check": "分析响应头安全配置，识别 CSP、HSTS、X-Content-Type-Options 等缺失项。",
    "form_extract": "被动解析 HTML 表单，识别 action、method 和输入字段，不提交表单。",
    "link_extract": "提取同源公开链接，为下一轮页面分析提供候选目标。",
    "browser_snapshot": "使用无头浏览器加载授权页面，获取 JavaScript 渲染后的可见文本、链接、表单和截图。",
    "dirsearch_scan": "调用标准 dirsearch 外部工具发现授权目标下的常见路径、目录和文件线索，扫描结果会写入证据账本供后续重规划使用。",
    "robots_analyzer": "读取并解析 robots.txt，发现站点主动声明的路径规则或隐藏目录线索。",
    "js_analyzer": "分析前端脚本中的接口、路由、关键词和潜在线索。",
    "path_normalizer": "将页面中发现的相对路径、静态资源和候选路径规范化为可分析 URL。",
    "flag_pattern_detector": "在已获取的公开文本中检测疑似 flag 或敏感标记。",
    "cookie_analyzer": "被动分析 Set-Cookie 安全属性，例如 HttpOnly、Secure 和 SameSite。",
    "sensitive_file_checker": "从已获取页面内容中识别备份文件、配置文件、源码泄露路径等公开线索。",
    "log_type_detector": "识别日志格式，为后续字段解析和异常检测选择合适策略。",
    "log_analyzer": "提取日志字段、异常请求、可疑来源和统计特征。",
    "attack_pattern_detector": "根据规则识别常见攻击行为，例如扫描、注入尝试或异常访问模式。",
    "timeline_builder": "将日志事件按时间组织，形成可追溯的事件链。",
    "project_detector": "识别源码项目类型、入口文件和主要技术栈。",
    "source_scanner": "静态扫描源码中的危险函数、输入入口和高风险代码片段。",
    "secret_scanner": "检测源码或配置中的硬编码密钥、令牌和敏感字符串。",
    "config_checker": "检查配置文件中的调试模式、弱口令、暴露服务和危险默认值。",
}

STOP_REASON_LABELS = {
    "evidence_sufficient": "证据已经满足当前任务的报告输出要求。",
    "missing evidence after maximum replans": "多轮重规划后仍缺少部分证据，系统停止继续扩展并输出阶段性报告。",
    "no new evidence after latest execution loop": "最近一轮执行后没有产生新的可追踪证据或线索，系统停止重复扩展并输出阶段性报告。",
}

SAFETY_MODE_REPORTS = {
    "conservative": (
        "保守模式：系统只自动执行低风险、被动、只读工具；中风险动作需要人工确认，"
        "高风险和禁止动作会被策略拦截。"
    ),
    "standard": (
        "标准模式：系统自动执行低风险工具；中风险验证类动作需要人工确认；"
        "高风险和禁止动作默认不执行。"
    ),
    "expert": (
        "专家模式：系统自动执行低风险工具；中风险和高风险授权验证动作必须经过人工确认；"
        "禁止动作仍然不可执行。"
    ),
}


class ReportSections(BaseModel):
    summary: str
    findings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ReportArtifact(BaseModel):
    content: str
    evidence_ids: list[str]
    report_type: str = "final"


class Reporter:
    def __init__(self, router: ModelRouter, ledger: LedgerService) -> None:
        self.router = router
        self.ledger = ledger

    async def render(
        self,
        task_id: str,
        parsed: ParsedTask,
        *,
        report_type: str = "final",
        stop_reason: str | None = None,
        safety_mode: str = "conservative",
        errors: list[str] | None = None,
    ) -> tuple[ReportArtifact, ModelResponse]:
        snapshot = self.ledger.snapshot(task_id)
        findings = self._model_findings(snapshot)
        response = await self.router.complete(
            ModelStage.REPORT,
            ModelRequest(
                system=(
                    "根据提供的证据账本生成中文结构化报告章节，不得补造证据。"
                    "findings、recommendations、uncertainties 必须使用中文表达。"
                    "如果证据不足，要明确说明缺少什么以及为什么不能下最终结论。"
                ),
                user=json.dumps(
                    {
                        "goal": parsed.goal,
                        "report_type": self._type_label(report_type),
                        "stop_reason": self._stop_reason_label(stop_reason),
                        "safety_policy": self._safety_mode_label(safety_mode),
                        "findings": findings,
                        "tool_chain": self._tool_chain_payload(snapshot),
                        "model_calls": self._model_call_payload(snapshot),
                        "recommendations": ["复核原始证据并按授权范围处置"],
                        "errors": errors or [],
                    },
                    ensure_ascii=False,
                ),
                response_schema=ReportSections.model_json_schema(),
            ),
        )
        sections = ReportSections.model_validate(response.data)
        artifact = self._compose_report(
            snapshot,
            parsed,
            report_type=report_type,
            stop_reason=stop_reason,
            safety_mode=safety_mode,
            sections=sections,
            errors=errors or [],
            demo=response.is_demo or any(item["is_demo"] for item in snapshot["model_calls"]),
        )
        return artifact, response

    def render_fallback(
        self,
        task_id: str,
        parsed: ParsedTask,
        *,
        report_type: str,
        stop_reason: str,
        safety_mode: str = "conservative",
        errors: list[str] | None = None,
    ) -> ReportArtifact:
        snapshot = self.ledger.snapshot(task_id)
        sections = ReportSections(
            summary="系统根据当前已持久化证据生成本报告；未补造任何未记录事实。",
            findings=[],
            recommendations=["复核证据账本、工具调用记录和任务详情页中的执行时间线。"],
            uncertainties=errors or ["任务未完全收敛，仍需结合证据账本判断后续方向。"],
            evidence_ids=[item["id"] for item in snapshot["evidences"]],
        )
        return self._compose_report(
            snapshot,
            parsed,
            report_type=report_type,
            stop_reason=stop_reason,
            safety_mode=safety_mode,
            sections=sections,
            errors=errors or [],
            demo=any(item["is_demo"] for item in snapshot["model_calls"]),
            fallback=True,
        )

    def _compose_report(
        self,
        snapshot: dict,
        parsed: ParsedTask,
        *,
        report_type: str,
        stop_reason: str | None,
        safety_mode: str,
        sections: ReportSections,
        errors: list[str],
        demo: bool,
        fallback: bool = False,
    ) -> ReportArtifact:
        allowed_ids = {item["id"] for item in snapshot["evidences"]}
        if sections.evidence_ids:
            cited_ids = sections.evidence_ids
            if len(cited_ids) != len(set(cited_ids)) or any(
                item not in allowed_ids for item in cited_ids
            ):
                raise ValueError("invalid evidence citation for current task")
        else:
            cited_ids = [item["id"] for item in snapshot["evidences"]]
        evidence_lines = self._evidence_lines(snapshot, cited_ids)
        finding_lines = [f"- {item}" for item in sections.findings] or ["- 暂无可独立确认的综合发现。"]
        recommendation_lines = [f"- {item}" for item in sections.recommendations] or ["- 暂无建议。"]
        uncertainty_lines = [f"- {item}" for item in (sections.uncertainties or errors)] or ["- 暂无。"]

        report = "\n\n".join(
            [
                self._title(report_type, demo),
                f"## 报告类型\n\n{self._type_label(report_type)}",
                f"## 安全策略\n\n{self._safety_mode_label(safety_mode)}",
                f"## 任务目标\n\n{parsed.goal}",
                f"## 停止原因\n\n{self._stop_reason_label(stop_reason)}",
                self._ai_decision_section(snapshot),
                f"## 摘要\n\n{sections.summary}",
                "## 关键发现\n\n" + "\n".join(finding_lines),
                "## 证据链\n\n" + "\n".join(evidence_lines),
                self._tool_chain_section(snapshot),
                self._model_call_section(snapshot),
                "## 处置建议\n\n" + "\n".join(recommendation_lines),
                "## 未完成项与不确定性\n\n" + "\n".join(uncertainty_lines),
                self._explanation_section(fallback),
            ]
        )
        return ReportArtifact(content=report, evidence_ids=cited_ids, report_type=report_type)

    def _model_findings(self, snapshot: dict) -> list[dict]:
        return [
            {
                "id": item["id"],
                "source": self._source_label(item["source"]),
                "raw_source": item["source"],
                "evidence_type": self._evidence_type_label(item["evidence_type"]),
                "content": item["content"],
                "confidence": item["confidence"],
            }
            for item in snapshot["evidences"]
        ]

    def _tool_chain_payload(self, snapshot: dict) -> list[dict]:
        return [
            {
                "tool_name": item["tool_name"],
                "tool_label": self._source_label(item["tool_name"]),
                "purpose": item.get("step_purpose") or self._tool_purpose(item["tool_name"]),
                "params": item.get("params", {}),
                "summary": item.get("result", {}).get("summary", item.get("status", "")),
                "status": item.get("status", ""),
            }
            for item in snapshot["tool_calls"]
        ]

    def _model_call_payload(self, snapshot: dict) -> list[dict]:
        return [
            {
                "stage": self._stage_label(item["stage"]),
                "provider": item["provider"],
                "status": self._status_label(item["status"]),
                "route_reason": item.get("route_reason"),
            }
            for item in snapshot["model_calls"]
        ]

    def _ai_decision_section(self, snapshot: dict) -> str:
        model_count = len(snapshot["model_calls"])
        tool_count = len(snapshot["tool_calls"])
        evidence_count = len(snapshot["evidences"])
        return (
            "## AI 决策说明\n\n"
            "- AI 不直接访问目标，也不直接执行命令；它负责理解用户目标、授权范围和安全策略，并生成或修正执行计划。\n"
            "- 系统执行器根据计划调用白名单工具，工具结果会写入证据账本，再作为后续复核、重规划和报告生成的依据。\n"
            f"- 本次任务已记录 {model_count} 次模型节点、{tool_count} 次工具调用、{evidence_count} 条证据。"
        )

    def _tool_chain_section(self, snapshot: dict) -> str:
        lines = []
        for index, item in enumerate(snapshot["tool_calls"], start=1):
            tool_name = item["tool_name"]
            result = item.get("result", {})
            summary = result.get("summary", item.get("status", "无摘要"))
            purpose = item.get("step_purpose") or self._tool_purpose(tool_name)
            step_name = item.get("step_name") or self._source_label(tool_name)
            lines.extend(
                [
                    f"- 第 {index} 步：{step_name}，调用 {self._source_label(tool_name)}（`{tool_name}`）",
                    f"- 调用原因：{purpose}",
                    "- 输入来源：来自任务目标、授权范围、上一轮工具输出或执行计划中的参数模板。",
                    f"- 关键结果：{summary}",
                    "- 对后续判断的作用：该结果会进入证据账本，供 Critic 判断证据是否充分，并供 Planner 生成后续计划时参考。",
                ]
            )
        if not lines:
            lines.append("- 暂无工具调用。")
        return "## 工具链执行说明\n\n" + "\n".join(lines)

    def _model_call_section(self, snapshot: dict) -> str:
        lines = [
            f"- {self._stage_label(item['stage'])} / `{item['provider']}`：{self._status_label(item['status'])}"
            for item in snapshot["model_calls"]
        ] or ["- 暂无模型调用。"]
        return "## 模型调用概览\n\n" + "\n".join(lines)

    def _evidence_lines(self, snapshot: dict, cited_ids: list[str]) -> list[str]:
        by_id = {item["id"]: item for item in snapshot["evidences"]}
        lines = []
        for evidence_id in cited_ids:
            item = by_id[evidence_id]
            lines.append(
                f"- `[{item['id']}]` {self._evidence_type_label(item['evidence_type'])}"
                f" / `{self._source_label(item['source'])}`：{item['content']}"
                f"（置信度 {item['confidence']:.2f}）"
            )
        return lines or ["- 暂无证据。"]

    def _explanation_section(self, fallback: bool) -> str:
        if fallback:
            return "## 说明\n\n该报告为兜底生成，事实来源以证据账本为准；未记录到证据账本的内容不会作为事实结论。"
        return "## 说明\n\n模型只负责解释、归纳和规划，事实来源以证据账本和工具调用结果为准。"

    @staticmethod
    def _title(report_type: str, demo: bool) -> str:
        prefix = "演示结果：" if demo else ""
        labels = {
            "final": "SecAgent-X 安全分析报告",
            "partial": "SecAgent-X 阶段性安全分析报告",
            "failed": "SecAgent-X 异常兜底安全分析报告",
        }
        return "# " + prefix + labels.get(report_type, "SecAgent-X 安全分析报告")

    @staticmethod
    def _type_label(report_type: str) -> str:
        labels = {
            "final": "最终报告：证据已满足当前任务输出要求。",
            "partial": "阶段性报告：任务未完全收敛，但已有证据可形成阶段性结论。",
            "failed": "异常兜底报告：任务执行异常，报告仅总结异常前已记录证据。",
        }
        return labels.get(report_type, report_type)

    @staticmethod
    def _safety_mode_label(value: str) -> str:
        return SAFETY_MODE_REPORTS.get(value, SAFETY_MODE_REPORTS["conservative"])

    @staticmethod
    def _evidence_type_label(value: str) -> str:
        return EVIDENCE_TYPE_LABELS.get(value, value)

    @staticmethod
    def _source_label(value: str) -> str:
        return TOOL_LABELS.get(value, value)

    @staticmethod
    def _tool_purpose(value: str) -> str:
        return TOOL_PURPOSES.get(value, "执行当前计划步骤所需的安全分析动作，并将结果写入证据账本。")

    @staticmethod
    def _stage_label(value: str) -> str:
        labels = {
            "task_parse": "任务理解",
            "plan": "计划生成",
            "critic": "证据复核",
            "report": "报告生成",
        }
        return labels.get(value, value)

    @staticmethod
    def _status_label(value: str) -> str:
        labels = {
            "completed": "成功",
            "error": "失败",
            "success": "成功",
            "failed": "失败",
        }
        return labels.get(value, value)

    @staticmethod
    def _stop_reason_label(value: str | None) -> str:
        if not value:
            return "证据已满足当前任务输出要求。"
        if value.startswith("budget exhausted: "):
            dimension = value.removeprefix("budget exhausted: ")
            dimensions = {
                "deadline": "任务执行时间达到上限",
                "model_calls": "模型调用次数达到上限",
                "input_tokens": "输入 Token 达到上限",
                "output_tokens": "输出 Token 达到上限",
                "steps": "执行步骤数量达到上限",
            }
            return dimensions.get(dimension, f"执行资源达到上限：{dimension}") + "，系统保留已取得证据并生成阶段性报告。"
        return STOP_REASON_LABELS.get(value, value)
