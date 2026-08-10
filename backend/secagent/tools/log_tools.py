import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from secagent.domain import RiskLevel, ToolResult
from secagent.tools.base import BaseTool, ToolContext

ACCESS_RE = re.compile(
    r'(?P<ip>\S+) \S+ \S+ \[(?P<time>[^]]+)] "(?P<method>\S+) '
    r'(?P<path>\S+) [^"]+" (?P<status>\d{3}) (?P<size>\S+)'
)
SENSITIVE = ("/admin", "/.git", "/backup", "/.env", "/wp-login")


def _authorized_path(raw_path: str, context: ToolContext) -> Path:
    path = Path(raw_path).resolve()
    workspace = context.workspace.resolve()
    if path != workspace and workspace not in path.parents:
        raise PermissionError("log path escapes task workspace")
    return path


def parse_access(path: Path, source_name: str | None = None) -> list[dict[str, Any]]:
    events = []
    label = source_name or path.name
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        match = ACCESS_RE.match(raw)
        if match:
            event = match.groupdict() | {
                "line": line_number,
                "raw_line": raw,
                "source": f"{label}:{line_number}",
                "evidence_type": "raw_line",
                "content": raw,
                "confidence": 1.0,
            }
            events.append(event)
    return events


def _events(params: dict, context: ToolContext) -> list[dict[str, Any]]:
    if "events" in params:
        return params["events"]
    path = _authorized_path(params["file_path"], context)
    return parse_access(path, params.get("source_name"))


class LogTypeDetector(BaseTool):
    name = "log_type_detector"
    scene = "incident_response"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        events = _events(params, context)
        return ToolResult(
            success=bool(events),
            summary="识别访问日志格式" if events else "无法识别日志格式",
            findings=[{"log_type": "nginx_combined" if events else "unknown"}],
        )


class LogAnalyzer(BaseTool):
    name = "log_analyzer"
    scene = "incident_response"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        events = _events(params, context)
        return ToolResult(
            success=bool(events),
            summary=f"解析 {len(events)} 条日志",
            metrics={
                "top_ips": Counter(event["ip"] for event in events).most_common(10),
                "status_codes": dict(
                    Counter(event["status"] for event in events)
                ),
            },
            evidence=events,
        )


class AttackPatternDetector(BaseTool):
    name = "attack_pattern_detector"
    scene = "incident_response"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        events = _events(params, context)
        grouped = Counter(
            event["ip"]
            for event in events
            if any(marker in event["path"] for marker in SENSITIVE)
        )
        findings = [
            {
                "rule_id": "WEB-SCAN-002",
                "ip": ip,
                "count": count,
                "reason": "短时间访问多个敏感路径",
                "confidence": min(0.99, 0.6 + count / 20),
            }
            for ip, count in grouped.items()
            if count >= 3
        ]
        evidence = [
            {
                "evidence_type": "rule_id",
                "source": f"rule:{item['rule_id']}",
                "content": (
                    f"{item['rule_id']} 命中：{item['ip']} 在样本中访问 "
                    f"{item['count']} 个敏感路径"
                ),
                "confidence": item["confidence"],
            }
            for item in findings
        ]
        return ToolResult(
            success=True,
            summary=f"发现 {len(findings)} 个扫描来源",
            findings=findings,
            evidence=evidence,
        )


class TimelineBuilder(BaseTool):
    name = "timeline_builder"
    scene = "incident_response"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        events = _events(params, context)
        ordered = sorted(
            events,
            key=lambda event: datetime.strptime(
                event["time"], "%d/%b/%Y:%H:%M:%S %z"
            ),
        )
        evidence = [
            event
            | {
                "evidence_type": "timeline",
                "content": (
                    f"{event['time']} {event['ip']} {event['method']} "
                    f"{event['path']} -> {event['status']}"
                ),
            }
            for event in ordered
        ]
        return ToolResult(
            success=True,
            summary=f"生成 {len(evidence)} 项时间线",
            evidence=evidence,
        )
