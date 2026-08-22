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
    "robots_analyzer": "robots.txt 分析",
    "js_analyzer": "前端脚本分析",
    "path_normalizer": "路径规范化",
    "flag_pattern_detector": "Flag 模式识别",
    "report_generator": "报告生成",
}

STOP_REASON_LABELS = {
    "evidence_sufficient": "证据已经满足当前任务的报告输出要求。",
    "missing evidence after maximum replans": "多轮重规划后仍缺少部分证据，系统停止继续扩展并输出阶段性报告。",
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
    findings: list = Field(default_factory=list)
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
        findings = [
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
        response = await self.router.complete(
            ModelStage.REPORT,
            ModelRequest(
                system=(
                    "根据提供的证据账本生成中文结构化报告章节，不得补造证据。"
                    "所有 findings、recommendations、uncertainties 必须使用中文表达。"
                    "如果证据不足，要明确说明缺少什么以及为什么不能下最终结论。"
                ),
                user=json.dumps(
                    {
                        "goal": parsed.goal,
                        "report_type": self._type_label(report_type),
                        "stop_reason": self._stop_reason_label(stop_reason),
                        "safety_policy": self._safety_mode_label(safety_mode),
                        "findings": findings,
                        "recommendations": ["复核原始证据并按授权范围处置"],
                        "errors": errors or [],
                    },
                    ensure_ascii=False,
                ),
                response_schema=ReportSections.model_json_schema(),
            ),
        )
        sections = ReportSections.model_validate(response.data)
        allowed_ids = {item["id"] for item in snapshot["evidences"]}
        cited_ids = sections.evidence_ids or [
            item["id"] for item in snapshot["evidences"]
        ]
        if len(cited_ids) != len(set(cited_ids)) or any(
            evidence_id not in allowed_ids for evidence_id in cited_ids
        ):
            raise ValueError("invalid evidence citation for current task")
        cited = {
            item["id"]: item
            for item in findings
            if item["id"] in set(cited_ids)
        }
        demo = response.is_demo or any(
            item["is_demo"] for item in snapshot["model_calls"]
        )
        title = self._title(report_type, demo)
        evidence_lines = [
            f"- `[{evidence_id}]` `{cited[evidence_id]['source']}`："
            f"{cited[evidence_id]['content']}（置信度 {cited[evidence_id]['confidence']:.2f}）"
            for evidence_id in cited_ids
        ] or ["- 暂无证据"]
        recommendation_lines = [
            f"- {item}" for item in sections.recommendations
        ] or ["- 暂无建议"]
        report = "\n\n".join(
            [
                title,
                f"## 报告类型\n\n{self._type_label(report_type)}",
                f"## 安全策略\n\n{self._safety_mode_label(safety_mode)}",
                f"## 停止原因\n\n{self._stop_reason_label(stop_reason)}",
                f"## 摘要\n\n{sections.summary}",
                "## 证据链\n\n" + "\n".join(evidence_lines),
                "## 处置建议\n\n" + "\n".join(recommendation_lines),
                "## 未完成项与不确定性\n\n"
                + "\n".join(f"- {item}" for item in (sections.uncertainties or errors or []))
                if (sections.uncertainties or errors)
                else "## 未完成项与不确定性\n\n- 暂无",
                "## 说明\n\n模型仅负责解释，事实来源以证据账本为准。",
            ]
        )
        return (
            ReportArtifact(
                content=report, evidence_ids=cited_ids, report_type=report_type
            ),
            response,
        )

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
        evidences = snapshot["evidences"]
        evidence_ids = [item["id"] for item in evidences]
        evidence_lines = [
            f"- `[{item['id']}]` {self._evidence_type_label(item['evidence_type'])}"
            f" / `{self._source_label(item['source'])}`：{item['content']}"
            f"（置信度 {item['confidence']:.2f}）"
            for item in evidences
        ] or ["- 暂无证据"]
        tool_lines = [
            f"- {self._source_label(item['tool_name'])}（`{item['tool_name']}`）："
            f"{item['result'].get('summary', item['status'])}"
            for item in snapshot["tool_calls"]
        ] or ["- 暂无工具调用"]
        model_lines = [
            f"- {self._stage_label(item['stage'])} / `{item['provider']}`：{self._status_label(item['status'])}"
            for item in snapshot["model_calls"]
        ] or ["- 暂无模型调用"]
        error_lines = [f"- {item}" for item in (errors or [])] or ["- 暂无"]
        report = "\n\n".join(
            [
                self._title(report_type, any(item["is_demo"] for item in snapshot["model_calls"])),
                f"## 报告类型\n\n{self._type_label(report_type)}",
                f"## 安全策略\n\n{self._safety_mode_label(safety_mode)}",
                f"## 任务目标\n\n{parsed.goal}",
                f"## 停止原因\n\n{self._stop_reason_label(stop_reason)}",
                "## 摘要\n\n系统根据当前已持久化证据生成本报告；未补造任何未记录事实。",
                "## 证据链\n\n" + "\n".join(evidence_lines),
                "## 工具执行概览\n\n" + "\n".join(tool_lines),
                "## 模型调用概览\n\n" + "\n".join(model_lines),
                "## 未完成项与不确定性\n\n" + "\n".join(error_lines),
                "## 说明\n\n该报告为兜底生成，事实来源以证据账本为准。",
            ]
        )
        return ReportArtifact(
            content=report,
            evidence_ids=evidence_ids,
            report_type=report_type,
        )

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
