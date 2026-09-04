"""submit_flag: record a found flag and end the solve loop."""

from __future__ import annotations

import re
from typing import Any

from secagent.domain import RiskLevel, ToolResult
from secagent.tools.base import BaseTool, ToolContext

_FLAG_RE = re.compile(
    r"(?i)\b(?:flag|ctf|nssctf|iscc|secagent)\{[^{}\s]{3,120}\}"
)


class SubmitFlag(BaseTool):
    name = "submit_flag"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True
    timeout_seconds = 10.0
    description = (
        "提交已找到的 flag 并结束本题。当你确定已经拿到完整 flag 字符串（形如 "
        "NSSCTF{...}、flag{...}、CTF{...}）时调用本工具；系统会记录该 flag 并"
        "立即结束本子任务。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "flag": {
                "type": "string",
                "description": "完整的 flag 字符串，如 NSSCTF{xxxxxxxx}",
            },
        },
        "required": ["flag"],
    }

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        del context
        flag = params.get("flag")
        if not isinstance(flag, str) or not flag.strip():
            return ToolResult(
                success=False, summary="缺少 flag 参数", error="invalid_flag"
            )
        flag = flag.strip()
        if not _FLAG_RE.fullmatch(flag):
            return ToolResult(
                success=False,
                summary=f"flag 格式不正确：{flag[:60]}",
                error="invalid_flag_format",
                warnings=["flag 需形如 NSSCTF{...} / flag{...} / CTF{...}"],
            )
        return ToolResult(
            success=True,
            summary=f"已提交 flag：{flag}",
            evidence=[
                {
                    "evidence_type": "flag_candidate",
                    "source": "submit_flag",
                    "content": f"提交 flag：{flag}",
                    "confidence": 1.0,
                    "metadata": {"candidate_flags": [flag]},
                }
            ],
        )


__all__ = ["SubmitFlag"]
