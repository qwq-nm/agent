import httpx
import pytest

from secagent.agents.runner import AgentRunner
from secagent.security.url_guard import UrlGuard
from secagent.tools.base import ToolContext
from secagent.tools.web_tools import (
    FlagPatternDetector,
    FormExtract,
    HeaderCheck,
    HttpFetch,
    JsAnalyzer,
    LinkExtract,
    PathNormalizer,
    RobotsAnalyzer,
)


@pytest.mark.asyncio
async def test_fetch_revalidates_redirect_target(tmp_path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/admin"},
        )

    transport = httpx.MockTransport(handler)
    tool = HttpFetch(
        UrlGuard(set(), resolver=lambda host: ["93.184.216.34"]),
        transport=transport,
    )
    with pytest.raises(Exception, match="blocked"):
        await tool.run(
            {"url": "https://example.com"},
            ToolContext("t1", "web_analysis", tmp_path),
        )
    assert calls == ["https://example.com"]


@pytest.mark.asyncio
async def test_header_check_accepts_http_fetch_metadata(tmp_path) -> None:
    result = await HeaderCheck().run(
        {
            "response": {
                "final_url": "https://example.com/",
                "headers": {"content-type": "text/html"},
                "body_preview": "<html></html>",
            }
        },
        ToolContext("t1", "web_analysis", tmp_path),
    )

    assert result.success is True
    assert any(item["header"] == "CSP" for item in result.findings)


@pytest.mark.asyncio
async def test_header_check_rejects_unresolved_http_placeholder(tmp_path) -> None:
    result = await HeaderCheck().run(
        {"response": "$http"},
        ToolContext("t1", "web_analysis", tmp_path),
    )

    assert result.success is False
    assert result.error == "invalid_http_response"


@pytest.mark.asyncio
async def test_form_extract_tolerates_missing_body_preview(tmp_path) -> None:
    result = await FormExtract().run(
        {
            "response": {
                "final_url": "https://example.com/",
                "headers": {"content-type": "text/html"},
            }
        },
        ToolContext("t1", "web_analysis", tmp_path),
    )

    assert result.success is True
    assert result.findings == []


def test_runner_resolves_http_placeholder_recursively(tmp_path) -> None:
    http_response = {
        "final_url": "https://example.com/",
        "headers": {"content-type": "text/html"},
        "body_preview": "<form></form>",
    }

    resolved = AgentRunner._resolve_params(
        {"response": "$http", "nested": {"copy": "$http"}},
        tmp_path,
        {"http_response": http_response},
    )

    assert resolved["response"] == http_response
    assert resolved["nested"]["copy"] == http_response


@pytest.mark.asyncio
async def test_link_extract_normalizes_same_origin_public_links(tmp_path) -> None:
    result = await LinkExtract().run(
        {
            "response": {
                "final_url": "https://example.com/app/",
                "headers": {"content-type": "text/html"},
                "body_preview": """
                <a href="/login">login</a>
                <script src="../static/app.js"></script>
                <form action="/search"></form>
                <a href="https://evil.test/offsite">offsite</a>
                """,
            }
        },
        ToolContext("t1", "web_analysis", tmp_path),
    )

    assert result.success is True
    urls = {item["url"] for item in result.findings}
    assert "https://example.com/login" in urls
    assert "https://example.com/static/app.js" in urls
    assert "https://example.com/search" in urls
    assert all("evil.test" not in item for item in urls)


@pytest.mark.asyncio
async def test_robots_analyzer_parses_public_directives(tmp_path) -> None:
    result = await RobotsAnalyzer().run(
        {
            "base_url": "https://example.com/",
            "response": {
                "final_url": "https://example.com/robots.txt",
                "headers": {"content-type": "text/plain"},
                "body_preview": "User-agent: *\nDisallow: /backup\nAllow: /public\n",
            },
        },
        ToolContext("t1", "web_analysis", tmp_path),
    )

    assert result.success is True
    values = {item["value"] for item in result.findings}
    assert "/backup" in values
    assert "/public" in values
    assert any("https://example.com/backup" in item["content"] for item in result.evidence)


@pytest.mark.asyncio
async def test_js_analyzer_extracts_route_hints_and_keywords(tmp_path) -> None:
    result = await JsAnalyzer().run(
        {
            "response": {
                "final_url": "https://example.com/static/app.js",
                "headers": {"content-type": "application/javascript"},
                "body_preview": "fetch('/api/flag'); const debug = true;",
            }
        },
        ToolContext("t1", "web_analysis", tmp_path),
    )

    assert result.success is True
    assert any(item.get("url") == "https://example.com/api/flag" for item in result.findings)
    assert any(item.get("keyword") == "debug" for item in result.findings)


@pytest.mark.asyncio
async def test_path_normalizer_extracts_same_origin_candidates(tmp_path) -> None:
    result = await PathNormalizer().run(
        {
            "base_url": "https://example.com/",
            "response": {
                "final_url": "https://example.com/",
                "headers": {"content-type": "text/html"},
                "body_preview": "try /admin and https://example.com/api/status",
            },
        },
        ToolContext("t1", "web_analysis", tmp_path),
    )

    assert result.success is True
    urls = {item["url"] for item in result.findings}
    assert "https://example.com/admin" in urls
    assert "https://example.com/api/status" in urls


@pytest.mark.asyncio
async def test_flag_pattern_detector_records_flag_like_evidence(tmp_path) -> None:
    result = await FlagPatternDetector().run(
        {
            "response": {
                "final_url": "https://example.com/",
                "headers": {"content-type": "text/html"},
                "body_preview": "hidden value is NSSCTF{passive_observation_only}",
            }
        },
        ToolContext("t1", "web_analysis", tmp_path),
    )

    assert result.success is True
    assert result.findings == [{"pattern": "NSSCTF{passive_observation_only}"}]
