import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

from secagent.domain import RiskLevel, ToolResult
from secagent.security.redaction import redact_text
from secagent.tools.base import BaseTool, ToolContext

SOURCE_RULES = {
    ".py": [
        (
            "PY-CMD-001",
            re.compile(
                r"\b(os\.system|eval|exec|pickle\.loads|subprocess\.(run|Popen))\s*\("
            ),
            "高风险执行函数",
        )
    ],
    ".php": [
        (
            "PHP-CMD-001",
            re.compile(r"\b(eval|exec|system|shell_exec)\s*\("),
            "高风险执行函数",
        )
    ],
    ".js": [
        ("JS-DOM-001", re.compile(r"\.innerHTML\s*="), "潜在 DOM 注入")
    ],
    ".ts": [
        ("TS-DOM-001", re.compile(r"\.innerHTML\s*="), "潜在 DOM 注入")
    ],
}
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*['\"]([^'\"]{8,})['\"]"
)
SKIP_DIRS = {".git", ".venv", "node_modules", "vendor"}


def _authorized_root(raw_path: str, context: ToolContext) -> Path:
    root = Path(raw_path).resolve()
    workspace = context.workspace.resolve()
    if root != workspace and workspace not in root.parents:
        raise PermissionError("source path escapes task workspace")
    return root


def source_files(
    root: Path,
    *,
    max_files: int = 2000,
    max_file_bytes: int = 1_000_000,
) -> Iterator[Path]:
    count = 0
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or path.stat().st_size > max_file_bytes
            or any(part in SKIP_DIRS for part in path.parts)
        ):
            continue
        count += 1
        if count > max_files:
            raise ValueError("source file limit exceeded")
        yield path


def finding(
    path: Path,
    line_number: int,
    rule_id: str,
    evidence: str,
    suggestion: str,
) -> dict[str, Any]:
    return {
        "file": str(path),
        "line": line_number,
        "rule_id": rule_id,
        "risk_level": "high",
        "evidence": redact_text(evidence.strip()),
        "suggestion": suggestion,
        "source": f"{path.name}:{line_number}",
    }


def evidence_for(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_type": "source_location",
        "source": item["source"],
        "content": (
            f"{item['rule_id']}：{item['evidence']}；建议：{item['suggestion']}"
        ),
        "confidence": 0.9,
    }


class ProjectDetector(BaseTool):
    name = "project_detector"
    scene = "source_audit"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        root = _authorized_root(params["project_path"], context)
        suffixes = Counter(path.suffix.lower() for path in source_files(root))
        language = (
            "python"
            if suffixes[".py"]
            else "javascript"
            if suffixes[".js"] or suffixes[".ts"]
            else "php"
            if suffixes[".php"]
            else "unknown"
        )
        return ToolResult(
            success=language != "unknown",
            summary=f"识别项目语言：{language}",
            findings=[{"language": language, "files_by_suffix": dict(suffixes)}],
        )


class SourceScanner(BaseTool):
    name = "source_scanner"
    scene = "source_audit"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        root = _authorized_root(params["project_path"], context)
        findings = []
        for path in source_files(root):
            rules = SOURCE_RULES.get(path.suffix.lower(), [])
            if not rules:
                continue
            for line_number, raw in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                for rule_id, pattern, suggestion in rules:
                    if pattern.search(raw):
                        findings.append(
                            finding(path, line_number, rule_id, raw, suggestion)
                        )
        return ToolResult(
            success=True,
            summary=f"发现 {len(findings)} 个源码风险模式",
            findings=findings,
            evidence=[evidence_for(item) for item in findings],
        )


class SecretScanner(BaseTool):
    name = "secret_scanner"
    scene = "source_audit"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        root = _authorized_root(params["project_path"], context)
        findings = []
        for path in source_files(root):
            for line_number, raw in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                if SECRET_RE.search(raw):
                    findings.append(
                        finding(
                            path,
                            line_number,
                            "SECRET-001",
                            raw,
                            "撤销该凭据并改用密钥管理服务",
                        )
                    )
        return ToolResult(
            success=True,
            summary=f"发现 {len(findings)} 个疑似硬编码凭据",
            findings=findings,
            evidence=[evidence_for(item) for item in findings],
        )


class ConfigChecker(BaseTool):
    name = "config_checker"
    scene = "source_audit"
    risk_level = RiskLevel.LOW
    idempotent = True

    RULES = (
        (
            "CFG-DEBUG-001",
            re.compile(r"\bDEBUG\s*=\s*True\b", re.IGNORECASE),
            "生产环境关闭调试模式",
        ),
        (
            "CFG-CORS-001",
            re.compile(r"(?:CORS|ORIGIN)[^\n]*['\"]\*['\"]", re.IGNORECASE),
            "将跨域来源限制为明确白名单",
        ),
        (
            "CFG-TLS-001",
            re.compile(r"verify\s*=\s*False", re.IGNORECASE),
            "启用 TLS 证书校验",
        ),
    )

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        root = _authorized_root(params["project_path"], context)
        findings = []
        for path in source_files(root):
            for line_number, raw in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                for rule_id, pattern, suggestion in self.RULES:
                    if pattern.search(raw):
                        findings.append(
                            finding(path, line_number, rule_id, raw, suggestion)
                        )
        return ToolResult(
            success=True,
            summary=f"发现 {len(findings)} 个配置风险",
            findings=findings,
            evidence=[evidence_for(item) for item in findings],
        )
