import json
from collections.abc import Mapping
from urllib.parse import urljoin, urlsplit

import httpx

from secagent.domain import RiskLevel, ToolResult
from secagent.security.redaction import redact_text
from secagent.security.url_guard import BlockedUrl, UrlGuard
from secagent.tools.base import BaseTool, ToolContext

_ALLOWED_METHODS = frozenset({"GET", "POST", "HEAD", "OPTIONS"})
_SENSITIVE_REQUEST_HEADERS = frozenset(
    {"authorization", "cookie", "proxy-authorization"}
)
_DEFAULT_MAX_REQUEST_BODY_BYTES = 256 * 1024
_DEFAULT_MAX_RESPONSE_BODY_BYTES = 1_000_000
_DEFAULT_TIMEOUT_SECONDS = 10.0


class HttpRequest(BaseTool):
    """Execute one bounded, explicitly authorized HTTP request."""

    # Keep the existing tool route stable while the implementation gains write support.
    name = "http_fetch"
    scene = "web_analysis"
    # Kept MEDIUM (active outbound request) by default; in autonomous mode the
    # ToolGateway auto-approves it so web/CTF analysis runs to a result.
    risk_level = RiskLevel.MEDIUM
    idempotent = False

    def __init__(
        self,
        guard: UrlGuard,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        max_redirects: int = 3,
        max_request_body_bytes: int = _DEFAULT_MAX_REQUEST_BODY_BYTES,
        max_response_body_bytes: int = _DEFAULT_MAX_RESPONSE_BODY_BYTES,
        max_body_bytes: int | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if max_redirects < 0:
            raise ValueError("max_redirects must be non-negative")
        if max_body_bytes is not None:
            max_response_body_bytes = max_body_bytes
        if max_request_body_bytes <= 0 or max_response_body_bytes <= 0:
            raise ValueError("HTTP body limits must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.guard = guard
        self.transport = transport
        self.max_redirects = max_redirects
        self.max_request_body_bytes = max_request_body_bytes
        self.max_response_body_bytes = max_response_body_bytes
        self.request_timeout_seconds = timeout_seconds
        self.timeout_seconds = timeout_seconds

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        try:
            return await self._run_request(params, context)
        except httpx.TimeoutException:
            return self._request_failure(params, "http_timeout")
        except httpx.RequestError:
            return self._request_failure(params, "http_transport_error")

    async def _run_request(
        self, params: dict, context: ToolContext
    ) -> ToolResult:
        del context
        current = self._require_url(params.get("url"))
        method = self._method(params.get("method", "GET"))
        query = self._query(params.get("query"))
        headers = self._headers(params.get("headers"))
        body = self._encode_body(params.get("body"))
        if len(body) > self.max_request_body_bytes:
            raise ValueError(
                "request body exceeds "
                f"{self.max_request_body_bytes} bytes"
            )

        redirects = 0
        first_request = True
        async with httpx.AsyncClient(
            transport=self.transport,
            follow_redirects=False,
            timeout=self.request_timeout_seconds,
            trust_env=False,
        ) as client:
            while True:
                self.guard.check(current)
                request_params = query if first_request else None
                request_body = body if method not in {"GET", "HEAD"} else None
                async with client.stream(
                    method,
                    current,
                    params=request_params,
                    headers=headers,
                    content=request_body,
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise BlockedUrl("blocked redirect without location")
                        if redirects >= self.max_redirects:
                            raise BlockedUrl("blocked redirect limit exceeded")
                        next_url = urljoin(current, location[:4096])
                        self.guard.check(next_url)
                        if self._host(current) != self._host(next_url):
                            headers = self._drop_sensitive_headers(headers)
                        if response.status_code in {301, 302, 303} and method != "HEAD":
                            method = "GET"
                            body = b""
                        current = next_url
                        redirects += 1
                        first_request = False
                        continue

                    content, truncated = await self._read_response(response)
                    encoding = response.encoding or "utf-8"
                    preview = redact_text(
                        content.decode(encoding, errors="replace"),
                        include_generic_key=True,
                    )
                    safe_url = redact_text(current, include_generic_key=True)
                    safe_headers = self._safe_response_headers(response.headers)
                    observation = {
                        "final_url": safe_url,
                        "status_code": response.status_code,
                        "headers": safe_headers,
                        "body_preview": preview,
                    }
                    if truncated:
                        observation["body_truncated"] = True
                    header_names = ", ".join(sorted(safe_headers))
                    warnings = ["响应体已截断"] if truncated else []
                    return ToolResult(
                        success=True,
                        summary=f"HTTP {response.status_code}",
                        evidence=[
                            {
                                "evidence_type": "http_observation",
                                "source": safe_url,
                                "content": (
                                    f"HTTP {response.status_code} {safe_url}; "
                                    f"headers={header_names}"
                                ),
                                "confidence": 1.0,
                                "metadata": observation,
                            }
                        ],
                        warnings=warnings,
                    )

    @staticmethod
    def _request_failure(params: dict, error_code: str) -> ToolResult:
        safe_url = redact_text(str(params.get("url", "")), include_generic_key=True)
        reason = {
            "http_timeout": "HTTP 请求超时，目标可能暂时不可达或响应过慢",
            "http_transport_error": "HTTP 请求在获得响应前失败，可能是目标不可达、端口不通、连接被拒绝或网络策略限制",
        }.get(error_code, f"HTTP 请求失败：{error_code}")
        return ToolResult(
            success=False,
            summary="HTTP 请求未获得可分析的响应",
            error=error_code,
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": safe_url,
                    "content": reason,
                    "confidence": 1.0,
                    "metadata": {
                        "final_url": safe_url,
                        "error_code": error_code,
                    },
                }
            ],
            warnings=["目标未返回可分析的 HTTP 响应"],
        )

    async def _read_response(
        self, response: httpx.Response
    ) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            remaining = self.max_response_body_bytes - size
            if remaining <= 0:
                return b"".join(chunks), True
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                return b"".join(chunks), True
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks), False

    @staticmethod
    def _require_url(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("url is required")
        return value

    @staticmethod
    def _method(value: object) -> str:
        method = str(value or "GET").upper()
        if method not in _ALLOWED_METHODS:
            raise ValueError(f"unsupported HTTP method: {method}")
        return method

    @staticmethod
    def _query(value: object) -> Mapping | list[tuple[str, object]] | None:
        if value is None:
            return None
        if isinstance(value, Mapping):
            return value
        if isinstance(value, list) and all(
            isinstance(item, (list, tuple)) and len(item) == 2 for item in value
        ):
            return value
        raise ValueError("query must be an object or list of pairs")

    @staticmethod
    def _headers(value: object) -> dict[str, str] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError("headers must be an object")
        headers: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, (str, int, float)):
                raise ValueError("headers must contain scalar string values")
            headers[key] = str(item)
        return headers

    def _encode_body(self, value: object) -> bytes:
        if value is None:
            return b""
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        if isinstance(value, (Mapping, list)):
            try:
                return json.dumps(
                    value, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValueError("body must be JSON serializable") from exc
        raise ValueError("body must be text, bytes, or JSON")

    @staticmethod
    def _host(url: str) -> str | None:
        return urlsplit(url).hostname

    @staticmethod
    def _drop_sensitive_headers(headers: dict[str, str] | None) -> dict[str, str] | None:
        if headers is None:
            return None
        return {
            key: value
            for key, value in headers.items()
            if key.lower() not in _SENSITIVE_REQUEST_HEADERS
        }

    @staticmethod
    def _safe_response_headers(headers: httpx.Headers) -> dict[str, str]:
        safe: dict[str, str] = {}
        for index, (key, value) in enumerate(headers.items()):
            if index >= 64:
                break
            normalized = key.lower()
            if normalized in {
                "authorization",
                "proxy-authenticate",
                "proxy-authorization",
                "set-cookie",
                "www-authenticate",
            }:
                safe[normalized] = "***REDACTED***"
            else:
                safe[normalized] = redact_text(value, include_generic_key=True)[:512]
        return safe


__all__ = ["HttpRequest"]
