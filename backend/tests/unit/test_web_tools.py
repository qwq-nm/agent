import httpx
import pytest

from secagent.agents.runner import AgentRunner
from secagent.security.url_guard import UrlGuard
from secagent.tools.base import ToolContext
from secagent.tools.web_tools import FormExtract, HeaderCheck, HttpFetch


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
