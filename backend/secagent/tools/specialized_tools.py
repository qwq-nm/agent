"""Read-only tools for the competition's vulnerability and reverse scenes."""

import re
from pathlib import Path
from typing import Any

from secagent.domain import RiskLevel, ToolResult
from secagent.security.redaction import redact_text
from secagent.tools.base import BaseTool, ToolContext
from secagent.tools.source_tools import _authorized_root, source_files


VULNERABILITY_RULES = (
    ("VULN-SQL-001", re.compile(r"(?:execute|query)\s*\([^\n]*(?:\+|format\(|f['\"])"), "possible SQL query construction from user-controlled input"),
    ("VULN-CMD-001", re.compile(r"(?:os\.system|subprocess\.(?:run|Popen)|child_process\.exec)\s*\("), "command execution boundary requires strict input validation"),
    ("VULN-XXE-001", re.compile(r"(?:xml\.etree|lxml|DocumentBuilder|SAXParser)", re.I), "XML parser usage requires an explicit external entity policy"),
    ("VULN-DESER-001", re.compile(r"(?:pickle\.loads|yaml\.load\s*\(|ObjectInputStream)", re.I), "deserialization boundary requires trusted input validation"),
    ("VULN-DEBUG-001", re.compile(r"(?:DEBUG\s*=\s*True|debug\s*[:=]\s*true)", re.I), "debug mode should be disabled in deployed environments"),
)


def _finding(path: Path, line: int, rule_id: str, raw: str, advice: str) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "file": str(path),
        "line": line,
        "evidence": redact_text(raw.strip()),
        "advice": advice,
        "source": f"{path.name}:{line}",
    }


class VulnerabilityScanner(BaseTool):
    name = "vulnerability_scanner"
    scene = "vulnerability_hunting"
    risk_level = RiskLevel.LOW
    idempotent = True
    description = "Scan authorized uploaded source artifacts for evidence-backed vulnerability patterns without executing code."

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        # The planner normally supplies $project. Falling back to the task
        # workspace keeps a valid plan executable when a provider omits the
        # optional parameter, while the authorization guard still prevents
        # access outside this task.
        root = _authorized_root(params.get("project_path", str(context.workspace)), context)
        findings: list[dict[str, Any]] = []
        for path in source_files(root):
            if path.suffix.lower() not in {".py", ".js", ".ts", ".php", ".java", ".xml", ".yml", ".yaml", ".json", ".conf"}:
                continue
            for line_number, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for rule_id, pattern, advice in VULNERABILITY_RULES:
                    if pattern.search(raw):
                        findings.append(_finding(path, line_number, rule_id, raw, advice))
        evidence = [{
            "evidence_type": "vulnerability_finding",
            "source": "vulnerability_scanner:summary",
            "content": "no vulnerability pattern matched in the authorized readable source set",
            "confidence": 0.7,
        }] if not findings else [{
            "evidence_type": "vulnerability_finding",
            "source": item["source"],
            "content": f"{item['rule_id']}: {item['evidence']}; advice: {item['advice']}",
            "confidence": 0.78,
        } for item in findings]
        return ToolResult(
            success=True,
            summary=f"identified {len(findings)} potential vulnerability patterns",
            findings=findings,
            evidence=evidence,
        )


class ReverseArtifactAnalyzer(BaseTool):
    name = "reverse_artifact_analyzer"
    scene = "reverse_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True
    description = "Perform bounded static triage of authorized artifacts; never imports or executes them."

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        root = _authorized_root(params.get("project_path", str(context.workspace)), context)
        findings: list[dict[str, Any]] = []
        for path in source_files(root, max_file_bytes=2_000_000):
            data = path.read_bytes()[:2_000_000]
            printable = "".join(chr(byte) if 32 <= byte < 127 else " " for byte in data)
            strings = [item for item in re.findall(r"[ -~]{6,}", printable) if item.strip()]
            markers = [item for item in strings if re.search(r"(?:http|token|secret|flag|debug|admin|password)", item, re.I)]
            artifact = {
                "file": str(path),
                "size": path.stat().st_size,
                "suffix": path.suffix.lower(),
                "printable_string_count": len(strings),
                "interesting_strings": [redact_text(item)[:160] for item in markers[:20]],
                "execution_performed": False,
                "imports_performed": False,
                "source": path.name,
            }
            findings.append(artifact)
        evidence = [{
            "evidence_type": "reverse_artifact",
            "source": "reverse_artifact_analyzer:summary",
            "content": "no authorized artifact was available for static triage",
            "confidence": 0.7,
        }] if not findings else [{
            "evidence_type": "reverse_artifact",
            "source": item["source"],
            "content": f"artifact={item['file']}; size={item['size']}; interesting_strings={item['interesting_strings']}; execution_performed=false",
            "confidence": 0.9,
        } for item in findings]
        return ToolResult(
            success=True,
            summary=f"statically triaged {len(findings)} authorized artifacts",
            findings=findings,
            evidence=evidence,
        )
