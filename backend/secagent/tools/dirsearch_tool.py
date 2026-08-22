import asyncio
import json
import re
import shutil
from typing import Any
from urllib.parse import urljoin, urlsplit

from secagent.domain import RiskLevel, ToolResult
from secagent.security.redaction import redact_text
from secagent.security.url_guard import UrlGuard
from secagent.tools.base import BaseTool, ToolContext


DEFAULT_EXTENSIONS = ("php", "html", "js", "txt", "json", "bak", "zip")


class DirsearchScan(BaseTool):
    name = "dirsearch_scan"
    scene = "web_analysis"
    risk_level = RiskLevel.MEDIUM
    idempotent = False
    requires_human_confirm = True
    timeout_seconds = 180.0
    description = (
        "调用标准 dirsearch 外部工具进行授权 Web 路径发现，结果写入证据账本。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "extensions": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["url"],
    }

    def __init__(self, guard: UrlGuard, command: str = "dirsearch") -> None:
        self.guard = guard
        self.command = command

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        url = self.guard.check(self._required_url(params.get("url"))).geturl()
        extensions = self._extensions(params.get("extensions"))
        output_file = context.workspace / "external-tools" / "dirsearch-result.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        command_path = shutil.which(self.command)
        if not command_path:
            return ToolResult(
                success=False,
                summary="dirsearch 外部工具未安装，无法执行路径发现",
                error="dirsearch_not_installed",
                warnings=[
                    "请在 API/Worker 运行环境安装 dirsearch，或重新构建包含 dirsearch 的 Docker 镜像。"
                ],
            )
        return await self._run_dirsearch(command_path, url, extensions, output_file)

    async def _run_dirsearch(
        self, command_path: str, url: str, extensions: list[str], output_file: Any
    ) -> ToolResult:
        if output_file.exists():
            output_file.unlink()
        commands = [
            [
                command_path,
                "-u",
                url,
                "-e",
                ",".join(extensions),
                "--format=json",
                "-o",
                str(output_file),
                "--threads=10",
                "--timeout=10",
                "--retries=1",
                "--no-color",
            ],
            [
                command_path,
                "-u",
                url,
                "-e",
                ",".join(extensions),
                "--output-formats=json",
                f"--output-file={output_file}",
                "--threads=10",
                "--timeout=10",
                "--retries=1",
                "--no-color",
            ],
        ]
        last_stderr = ""
        last_stdout = ""
        for args in commands:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=150
                )
            except TimeoutError:
                process.kill()
                await process.communicate()
                return ToolResult(
                    success=False,
                    summary="dirsearch 执行超时，未获得完整扫描结果",
                    error="dirsearch_timeout",
                    warnings=["外部工具超过本系统设置的 150 秒上限。"],
                )
            last_stdout = stdout.decode("utf-8", errors="replace")[:20000]
            last_stderr = stderr.decode("utf-8", errors="replace")[:4000]
            if process.returncode == 0:
                findings = self._findings_from_json(output_file, url)
                if not findings:
                    findings = self._findings_from_text(last_stdout, url)
                return self._result_from_findings(
                    url,
                    findings,
                    metrics={
                        "command": " ".join(args[:2] + ["***"]),
                        "extensions": extensions,
                        "return_code": process.returncode,
                    },
                )
            if "unrecognized arguments" not in last_stderr.lower():
                break
        return ToolResult(
            success=False,
            summary="dirsearch 外部命令执行失败，未获得可用扫描结果",
            error="dirsearch_failed",
            warnings=[
                redact_text(last_stderr or last_stdout, include_generic_key=True)[
                    :2000
                ]
            ],
        )

    @staticmethod
    def _required_url(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("url is required")
        return value.strip()

    @staticmethod
    def _extensions(value: object) -> list[str]:
        if not isinstance(value, list):
            return list(DEFAULT_EXTENSIONS)
        cleaned = []
        for item in value:
            text = str(item).strip().lstrip(".").lower()
            if re.fullmatch(r"[a-z0-9]{1,12}", text):
                cleaned.append(text)
        return cleaned[:20] or list(DEFAULT_EXTENSIONS)

    def _findings_from_json(self, output_file: Any, base_url: str) -> list[dict[str, Any]]:
        if not output_file.exists():
            return []
        try:
            data = json.loads(output_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        findings: list[dict[str, Any]] = []
        self._walk_json(data, findings, base_url)
        return findings

    def _walk_json(
        self, node: Any, findings: list[dict[str, Any]], base_url: str
    ) -> None:
        if isinstance(node, dict):
            url = node.get("url") or node.get("path") or node.get("target")
            status = node.get("status") or node.get("status_code")
            if isinstance(url, str):
                full_url = (
                    url
                    if url.startswith(("http://", "https://"))
                    else urljoin(base_url, url)
                )
                if self._same_origin(base_url, full_url):
                    findings.append(
                        {
                            "url": redact_text(full_url, include_generic_key=True),
                            "status": self._int_or_none(status),
                            "length": self._int_or_none(
                                node.get("length") or node.get("content_length")
                            ),
                            "reason": "dirsearch 路径发现",
                        }
                    )
            for value in node.values():
                self._walk_json(value, findings, base_url)
        elif isinstance(node, list):
            for value in node:
                self._walk_json(value, findings, base_url)

    def _findings_from_text(self, text: str, base_url: str) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        pattern = re.compile(r"(?P<status>[1-5][0-9]{2})[^\n]*(?P<url>https?://[^\s]+)")
        for match in pattern.finditer(text):
            url = match.group("url")
            if self._same_origin(base_url, url):
                findings.append(
                    {
                        "url": redact_text(url, include_generic_key=True),
                        "status": int(match.group("status")),
                        "length": None,
                        "reason": "dirsearch 输出解析",
                    }
                )
        return findings

    @staticmethod
    def _same_origin(base_url: str, candidate: str) -> bool:
        base = urlsplit(base_url)
        parsed = urlsplit(candidate)
        return base.scheme == parsed.scheme and base.netloc == parsed.netloc

    @staticmethod
    def _int_or_none(value: object) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    @staticmethod
    def _result_from_findings(
        url: str, findings: list[dict[str, Any]], *, metrics: dict[str, Any]
    ) -> ToolResult:
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in findings:
            target = str(item.get("url") or "")
            if not target or target in seen:
                continue
            seen.add(target)
            unique.append(item)
        return ToolResult(
            success=True,
            summary=f"dirsearch 扫描完成，发现 {len(unique)} 个可关注路径",
            findings=unique,
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": str(item["url"]),
                    "content": (
                        f"dirsearch 发现路径：{item['url']}，状态码 "
                        f"{item.get('status') or '未知'}。"
                    ),
                    "confidence": 0.88,
                    "metadata": item,
                }
                for item in unique
            ],
            metrics={**metrics, "engine": "dirsearch", "base_url": url},
        )


__all__ = ["DirsearchScan"]
