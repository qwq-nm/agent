from pathlib import Path

import pytest

from secagent.tools.base import ToolContext
from secagent.tools.specialized_tools import (
    ReverseArtifactAnalyzer,
    VulnerabilityScanner,
)
from secagent.worker import build_worker_registry


@pytest.mark.asyncio
async def test_vulnerability_scanner_reports_line_backed_findings(tmp_path: Path) -> None:
    sample = tmp_path / "app.py"
    sample.write_text("import subprocess\nsubprocess.run(user_input, shell=True)\n", encoding="utf-8")

    result = await VulnerabilityScanner().run(
        {"project_path": str(tmp_path)},
        ToolContext("task-1", "vulnerability_hunting", tmp_path),
    )

    assert result.success is True
    assert result.findings[0]["rule_id"] == "VULN-CMD-001"
    assert result.evidence[0]["source"] == "app.py:2"


@pytest.mark.asyncio
async def test_reverse_analyzer_never_executes_artifact(tmp_path: Path) -> None:
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"MZ\x00\x00https://example.test secret_token\x00")

    result = await ReverseArtifactAnalyzer().run(
        {"project_path": str(tmp_path)},
        ToolContext("task-1", "reverse_analysis", tmp_path),
    )

    assert result.success is True
    assert result.findings[0]["execution_performed"] is False
    assert result.findings[0]["imports_performed"] is False
    assert result.evidence[0]["evidence_type"] == "reverse_artifact"


@pytest.mark.asyncio
async def test_specialized_tools_reject_workspace_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-specialized-sample.py"
    outside.write_text("DEBUG = True\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(PermissionError):
        await VulnerabilityScanner().run(
            {"project_path": str(outside.parent)},
            ToolContext("task-1", "vulnerability_hunting", workspace),
        )


def test_worker_registry_includes_specialized_scene_tools() -> None:
    names = {item["name"] for item in build_worker_registry(set()).describe()}
    assert {"vulnerability_scanner", "reverse_artifact_analyzer"} <= names
