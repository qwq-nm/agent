import httpx
import pytest

from secagent.security.url_guard import UrlGuard
from secagent.tools.base import ToolContext
from secagent.tools.web_tools import HttpFetch


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
