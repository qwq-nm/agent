import pytest

from secagent.tools.base import ToolContext
from secagent.tools.log_tools import (
    AttackPatternDetector,
    LogAnalyzer,
    LogTypeDetector,
    TimelineBuilder,
)


@pytest.mark.asyncio
async def test_log_chain_detects_known_scan(tmp_path) -> None:
    log = tmp_path / "access.log"
    log.write_text(
        '203.0.113.24 - - [10/Aug/2026:12:00:00 +0000] "GET /admin HTTP/1.1" 404 10\n'
        '203.0.113.24 - - [10/Aug/2026:12:00:01 +0000] "GET /.git/config HTTP/1.1" 404 10\n'
        '203.0.113.24 - - [10/Aug/2026:12:00:02 +0000] "GET /backup.zip HTTP/1.1" 404 10\n',
        encoding="utf-8",
    )
    context = ToolContext("t1", "incident_response", tmp_path)
    detected = await LogTypeDetector().run({"file_path": str(log)}, context)
    assert detected.findings[0]["log_type"] == "nginx_combined"
    analysis = await LogAnalyzer().run({"file_path": str(log)}, context)
    patterns = await AttackPatternDetector().run(
        {"events": analysis.evidence}, context
    )
    timeline = await TimelineBuilder().run({"events": analysis.evidence}, context)
    assert patterns.findings[0]["rule_id"] == "WEB-SCAN-002"
    assert timeline.evidence[0]["source"].startswith("access.log:")
