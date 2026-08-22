import asyncio
import re
import shutil
from typing import Any

from secagent.domain import RiskLevel, ToolResult
from secagent.security.redaction import redact_text
from secagent.security.url_guard import UrlGuard
from secagent.tools.base import BaseTool, ToolContext


class SqlmapProbe(BaseTool):
    name = "sqlmap_probe"
    scene = "web_analysis"
    risk_level = RiskLevel.HIGH
    idempotent = False
    requires_human_confirm = True
    timeout_seconds = 240.0
    description = (
        "调用原版 sqlmap 外部工具，对授权目标进行 SQL 注入探测；"
        "该动作风险较高，必须经过人工确认后执行。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "level": {"type": "integer", "minimum": 1, "maximum": 5},
            "risk": {"type": "integer", "minimum": 1, "maximum": 3},
            "extra_args": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["url"],
    }

    def __init__(self, guard: UrlGuard, command: str = "sqlmap") -> None:
        self.guard = guard
        self.command = command

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        url = self.guard.check(self._required_url(params.get("url"))).geturl()
        command_path = shutil.which(self.command)
        if not command_path:
            return ToolResult(
                success=False,
                summary="未找到 sqlmap 命令，无法执行 SQL 注入探测。",
                error="sqlmap_not_installed",
                warnings=[
                    "请在 API/Worker 运行环境安装原版 sqlmap，或重新构建包含 sqlmap 的 Docker 镜像。"
                ],
            )
        output_dir = context.workspace / "external-tools" / "sqlmap"
        output_dir.mkdir(parents=True, exist_ok=True)
        args = [
            command_path,
            "-u",
            url,
            "--batch",
            "--level",
            str(self._bounded_int(params.get("level"), default=1, minimum=1, maximum=5)),
            "--risk",
            str(self._bounded_int(params.get("risk"), default=1, minimum=1, maximum=3)),
            "--output-dir",
            str(output_dir),
        ]
        args.extend(self._extra_args(params.get("extra_args")))
        return await self._run_sqlmap(args, url)

    async def _run_sqlmap(self, args: list[str], url: str) -> ToolResult:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=220)
        except TimeoutError:
            process.kill()
            await process.communicate()
            return ToolResult(
                success=False,
                summary="sqlmap 执行超时，未获得完整探测结果。",
                error="sqlmap_timeout",
                warnings=["原版 sqlmap 运行超过本系统设置的 220 秒上限。"],
            )

        text = "\n".join(
            part.decode("utf-8", errors="replace")
            for part in (stdout, stderr)
            if part
        )
        safe_output = redact_text(text, include_generic_key=True)
        findings = self._parse_findings(safe_output)
        vulnerable = any(item.get("kind") == "vulnerable" for item in findings)
        if process.returncode not in {0, 1} and not findings:
            return ToolResult(
                success=False,
                summary="sqlmap 命令执行失败，未获得可用探测结果。",
                error="sqlmap_failed",
                warnings=[safe_output[-3000:] or f"sqlmap exit code {process.returncode}"],
            )
        conclusion = "发现疑似 SQL 注入风险" if vulnerable else "未确认 SQL 注入风险"
        return ToolResult(
            success=True,
            summary=f"sqlmap 探测完成：{conclusion}。",
            findings=findings,
            evidence=[
                {
                    "evidence_type": "sqlmap_probe",
                    "source": redact_text(url, include_generic_key=True),
                    "content": self._evidence_content(conclusion, findings, safe_output),
                    "confidence": 0.78 if vulnerable else 0.62,
                    "metadata": {
                        "url": redact_text(url, include_generic_key=True),
                        "vulnerable": vulnerable,
                        "return_code": process.returncode,
                        "output_preview": safe_output[-6000:],
                    },
                }
            ],
            metrics={
                "engine": "sqlmap",
                "return_code": process.returncode,
                "vulnerable": vulnerable,
            },
            warnings=[
                "该结果来自原版 sqlmap 外部工具，属于高风险授权验证动作，已要求人工确认。"
            ],
        )

    @staticmethod
    def _required_url(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("url is required")
        return value.strip()

    @staticmethod
    def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, number))

    @staticmethod
    def _extra_args(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        args: list[str] = []
        for item in value:
            text = str(item).strip()
            if not text or "\x00" in text:
                continue
            args.append(text)
        return args[:20]

    @staticmethod
    def _parse_findings(output: str) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        if re.search(r"\bis vulnerable\b", output, flags=re.IGNORECASE):
            findings.append(
                {
                    "kind": "vulnerable",
                    "description": "sqlmap 输出显示目标存在可疑注入点。",
                }
            )
        for match in re.finditer(r"Parameter:\s*([^\n]+)", output, re.IGNORECASE):
            findings.append(
                {
                    "kind": "parameter",
                    "parameter": match.group(1).strip()[:200],
                    "description": "sqlmap 识别到可测试或疑似存在风险的参数。",
                }
            )
        for match in re.finditer(r"Type:\s*([^\n]+)", output, re.IGNORECASE):
            findings.append(
                {
                    "kind": "injection_type",
                    "type": match.group(1).strip()[:200],
                    "description": "sqlmap 输出中的注入类型线索。",
                }
            )
        if "all tested parameters do not appear to be injectable" in output.lower():
            findings.append(
                {
                    "kind": "not_injectable",
                    "description": "sqlmap 未确认当前参数存在 SQL 注入。",
                }
            )
        if "no parameter(s) found for testing" in output.lower():
            findings.append(
                {
                    "kind": "no_parameters",
                    "description": "sqlmap 未在当前 URL 中找到可测试参数。",
                }
            )
        return findings[:30]

    @staticmethod
    def _evidence_content(
        conclusion: str, findings: list[dict[str, Any]], output: str
    ) -> str:
        lines = [f"sqlmap 探测结论：{conclusion}。"]
        if findings:
            for item in findings[:8]:
                lines.append(f"- {item.get('description') or item}")
        lines.append("原版 sqlmap 输出摘要：")
        lines.append(output[-2000:] if output else "无输出。")
        return "\n".join(lines)


__all__ = ["SqlmapProbe"]
