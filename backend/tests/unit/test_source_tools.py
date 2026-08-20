import pytest

from secagent.tools.base import ToolContext
from secagent.tools.source_tools import (
    ConfigChecker,
    ProjectDetector,
    SecretScanner,
    SourceScanner,
)


@pytest.mark.asyncio
async def test_static_tools_find_seeded_python_risks_without_execution(tmp_path) -> None:
    source = tmp_path / "app.py"
    source.write_text(
        'import os\nAPI_KEY = "sk-demo-not-real-123456"\n'
        'def run(cmd):\n    return os.system(cmd)\n',
        encoding="utf-8",
    )
    config = tmp_path / "config.py"
    config.write_text("DEBUG = True\n", encoding="utf-8")
    context = ToolContext("t1", "source_audit", tmp_path)
    project = await ProjectDetector().run(
        {"project_path": str(tmp_path)}, context
    )
    assert project.findings[0]["language"] == "python"
    source_result = await SourceScanner().run(
        {"project_path": str(tmp_path)}, context
    )
    secret_result = await SecretScanner().run(
        {"project_path": str(tmp_path)}, context
    )
    config_result = await ConfigChecker().run(
        {"project_path": str(tmp_path)}, context
    )
    assert source_result.findings[0]["rule_id"] == "PY-CMD-001"
    assert "sk-demo-not-real-123456" not in str(secret_result.model_dump())
    assert config_result.findings[0]["rule_id"] == "CFG-DEBUG-001"
