"""Raw sandbox tools for an authorized CTF/web target.

These give the model the ability to compose temporary operations — run a
bounded diagnostic command, read/write a scratch file, list files — inside the
task workspace, instead of only the pre-packaged web tools. They run as host
subprocesses (no container isolation), so the safety story is:

* ``sandbox_bash`` is HIGH risk: RiskGate requires human approval (and rejects
  it outright in conservative/standard mode).
* Every path is pinned to the task workspace; path traversal is rejected.
* Metadata-service addresses are blocked outright.
* Command, output and duration are written to the evidence ledger via the
  normal ``ToolResult.evidence`` flow.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from secagent.domain import RiskLevel, ToolResult
from secagent.tools.base import BaseTool, ToolContext

_METADATA_MARKERS = (
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.azure.internal",
    "metadata.internal",
)
_MAX_OUTPUT_CHARS = 20_000
_MAX_FILE_BYTES = 512 * 1024


def _resolve_in_workspace(workspace: Path, rel: str) -> Path:
    raw = Path(rel)
    candidate = (raw if raw.is_absolute() else workspace / raw).resolve()
    root = workspace.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path escapes the task workspace")
    return candidate


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _evidence(
    source: str, content: str, *, metadata: dict[str, Any] | None = None, confidence: float = 1.0
) -> dict[str, Any]:
    return {
        "evidence_type": "sandbox_observation",
        "source": source,
        "content": content,
        "confidence": confidence,
        "metadata": metadata or {},
    }


class SandboxBash(BaseTool):
    name = "sandbox_bash"
    scene = "web_analysis"
    risk_level = RiskLevel.MEDIUM
    idempotent = False
    requires_human_confirm = True
    timeout_seconds = 120.0
    description = (
        "在任务工作目录内执行一条诊断命令（如 curl 授权目标、解析下载的文件）。"
        "是否执行由系统安全策略决定；命令、输出、耗时全部写入证据账本。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
        },
        "required": ["command"],
    }

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult(
                success=False,
                summary="sandbox_bash 缺少 command 参数",
                error="invalid_command",
            )
        if any(marker in command for marker in _METADATA_MARKERS):
            return ToolResult(
                success=False,
                summary="命令命中元数据服务地址，已拒绝执行",
                error="metadata_blocked",
                warnings=["禁止访问云元数据地址。"],
            )

        per_call_timeout = _bounded_int(
            params.get("timeout_seconds"), default=30, minimum=1, maximum=120
        )
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            "sh",
            "-c",
            command,
            cwd=str(context.workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=per_call_timeout
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return ToolResult(
                success=False,
                summary="sandbox_bash 执行超时",
                error="sandbox_timeout",
                warnings=[f"命令超过 {per_call_timeout} 秒上限，已终止。"],
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        output = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")[
            :_MAX_OUTPUT_CHARS
        ]
        success = process.returncode == 0
        return ToolResult(
            success=success,
            summary=(
                f"sandbox_bash 执行{'成功' if success else '失败'}，退出码 {process.returncode}，"
                f"输出 {len(output)} 字符。"
            ),
            evidence=[
                _evidence(
                    "sandbox_bash",
                    output or "(无输出)",
                    metadata={
                        "command": command[:2000],
                        "return_code": process.returncode,
                        "duration_ms": duration_ms,
                    },
                )
            ],
            metrics={
                "engine": "sandbox_bash",
                "return_code": process.returncode,
                "duration_ms": duration_ms,
            },
            warnings=["sandbox_bash 会执行命令，已要求人工确认。"],
        )


class SandboxReadFile(BaseTool):
    name = "sandbox_read_file"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True
    timeout_seconds = 15.0
    description = "读取任务工作目录内的一个文件（限制大小），用于查看下载的源码或中间产物。"
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        path = params.get("path")
        if not isinstance(path, str) or not path.strip():
            return ToolResult(success=False, summary="缺少 path", error="invalid_path")
        try:
            target = _resolve_in_workspace(context.workspace, path)
        except ValueError as exc:
            return ToolResult(success=False, summary=str(exc), error="path_escape")
        if not target.is_file():
            return ToolResult(success=False, summary=f"文件不存在：{target.name}", error="file_not_found")
        if target.stat().st_size > _MAX_FILE_BYTES:
            return ToolResult(
                success=False,
                summary="文件超过读取大小上限",
                error="file_too_large",
            )
        content = target.read_text(encoding="utf-8", errors="replace")
        return ToolResult(
            success=True,
            summary=f"读取文件 {target.name}，{len(content)} 字符。",
            evidence=[
                _evidence(
                    "sandbox_read_file",
                    content[:_MAX_OUTPUT_CHARS],
                    metadata={"path": target.name},
                )
            ],
        )


class SandboxWriteFile(BaseTool):
    name = "sandbox_write_file"
    scene = "web_analysis"
    risk_level = RiskLevel.MEDIUM
    idempotent = False
    requires_human_confirm = True
    timeout_seconds = 15.0
    description = "在任务工作目录内写入一个文件，用于保存脚本或中间产物；是否执行由系统安全策略决定。"
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        path = params.get("path")
        content = params.get("content")
        if not isinstance(path, str) or not path.strip():
            return ToolResult(success=False, summary="缺少 path", error="invalid_path")
        if not isinstance(content, str):
            return ToolResult(success=False, summary="content 必须是字符串", error="invalid_content")
        try:
            target = _resolve_in_workspace(context.workspace, path)
        except ValueError as exc:
            return ToolResult(success=False, summary=str(exc), error="path_escape")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(
            success=True,
            summary=f"写入文件 {target.name}，{len(content)} 字符。",
            evidence=[
                _evidence(
                    "sandbox_write_file",
                    f"wrote {target.name} ({len(content)} chars)",
                    metadata={"path": target.name},
                )
            ],
            warnings=["sandbox_write_file 会修改工作目录文件，已要求人工确认。"],
        )


class SandboxListFiles(BaseTool):
    name = "sandbox_list_files"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True
    timeout_seconds = 15.0
    description = "列出任务工作目录内的文件，用于了解已下载/生成的中间产物。"
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    }

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        path = params.get("path") or "."
        try:
            target = _resolve_in_workspace(context.workspace, str(path))
        except ValueError as exc:
            return ToolResult(success=False, summary=str(exc), error="path_escape")
        if not target.is_dir():
            return ToolResult(success=False, summary=f"目录不存在：{target}", error="dir_not_found")
        entries = sorted(
            f"{child.name}{'/' if child.is_dir() else ''}" for child in target.iterdir()
        )[:200]
        content = "\n".join(entries) or "(空目录)"
        return ToolResult(
            success=True,
            summary=f"列出 {len(entries)} 个条目。",
            evidence=[
                _evidence(
                    "sandbox_list_files",
                    content,
                    metadata={"path": str(target.relative_to(context.workspace.resolve())) or "."},
                )
            ],
        )


__all__ = ["SandboxBash", "SandboxReadFile", "SandboxWriteFile", "SandboxListFiles"]
