import httpx
import pytest

from secagent.security.url_guard import BlockedUrl, UrlGuard
from secagent.security.redaction import redact_mapping
from secagent.tools.base import ToolContext
from secagent.tools.http_request import HttpRequest


@pytest.mark.asyncio
async def test_http_request_supports_authorized_post_and_redacts_observations(tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"set-cookie": "session=private-cookie"},
            text='token=secret-value <html>ok</html>',
        )

    tool = HttpRequest(
        UrlGuard({"target.test"}, resolver=lambda host: ["192.0.2.10"]),
        transport=httpx.MockTransport(handler),
    )

    result = await tool.run(
        {
            "url": "https://target.test/submit",
            "method": "POST",
            "query": {"next": "1"},
            "headers": {"Cookie": "session=private-cookie"},
            "body": "token=secret-value",
        },
        ToolContext("task-1", "web_analysis", tmp_path),
    )

    assert result.success is True
    assert seen[0].method == "POST"
    assert str(seen[0].url) == "https://target.test/submit?next=1"
    assert seen[0].content == b"token=secret-value"
    observation = result.evidence[0]["metadata"]
    assert observation["status_code"] == 200
    assert "secret-value" not in observation["body_preview"]
    assert observation["headers"]["set-cookie"] == "***REDACTED***"


@pytest.mark.asyncio
async def test_http_request_revalidates_redirect_target(tmp_path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    tool = HttpRequest(
        UrlGuard({"target.test"}, resolver=lambda host: ["192.0.2.10"]),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(BlockedUrl, match="blocked"):
        await tool.run(
            {"url": "https://target.test/"},
            ToolContext("task-1", "web_analysis", tmp_path),
        )

    assert calls == ["https://target.test/"]


@pytest.mark.asyncio
async def test_http_request_rejects_unsupported_method(tmp_path) -> None:
    tool = HttpRequest(
        UrlGuard({"target.test"}),
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
    )

    with pytest.raises(ValueError, match="unsupported HTTP method"):
        await tool.run(
            {"url": "https://target.test/", "method": "TRACE"},
            ToolContext("task-1", "web_analysis", tmp_path),
        )


@pytest.mark.asyncio
async def test_http_request_rejects_oversized_request_body(tmp_path) -> None:
    tool = HttpRequest(
        UrlGuard({"target.test"}, resolver=lambda host: ["192.0.2.10"]),
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        max_request_body_bytes=4,
    )

    with pytest.raises(ValueError, match="request body exceeds"):
        await tool.run(
            {"url": "https://target.test/", "method": "POST", "body": "12345"},
            ToolContext("task-1", "web_analysis", tmp_path),
        )


@pytest.mark.asyncio
async def test_http_request_bounds_response_body_preview(tmp_path) -> None:
    tool = HttpRequest(
        UrlGuard({"target.test"}, resolver=lambda host: ["192.0.2.10"]),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"123456789")
        ),
        max_response_body_bytes=4,
    )

    result = await tool.run(
        {"url": "https://target.test/"},
        ToolContext("task-1", "web_analysis", tmp_path),
    )

    observation = result.evidence[0]["metadata"]
    assert observation["body_preview"] == "1234"
    assert "响应体已截断" in result.warnings


@pytest.mark.asyncio
async def test_http_request_returns_redacted_transport_failure(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError(
            "server exposed token=transport-secret",
            request=request,
        )

    tool = HttpRequest(
        UrlGuard({"target.test"}, resolver=lambda host: ["192.0.2.10"]),
        transport=httpx.MockTransport(handler),
    )

    result = await tool.run(
        {"url": "https://target.test/secret.php"},
        ToolContext("task-1", "web_analysis", tmp_path),
    )

    assert result.success is False
    assert result.error == "http_transport_error"
    assert result.evidence[0]["metadata"]["error_code"] == "http_transport_error"
    assert "transport-secret" not in result.model_dump_json()


def test_http_request_params_drop_raw_body_before_persistence() -> None:
    safe = redact_mapping(
        {
            "url": "https://target.test/submit",
            "headers": {"Cookie": "session=private-cookie"},
            "body": "ordinary form content that must not be stored",
        }
    )

    assert safe["headers"]["Cookie"] == "***REDACTED***"
    assert safe["body"] == "***REDACTED***"
