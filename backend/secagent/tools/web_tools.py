from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from secagent.domain import RiskLevel, ToolResult
from secagent.security.url_guard import BlockedUrl, UrlGuard
from secagent.tools.base import BaseTool, ToolContext


class UrlGuardTool(BaseTool):
    name = "url_guard"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True

    def __init__(self, guard: UrlGuard) -> None:
        self.guard = guard

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        parsed = self.guard.check(params["url"])
        normalized = parsed.geturl()
        return ToolResult(
            success=True,
            summary="目标 URL 通过 SSRF 策略检查",
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": normalized,
                    "content": f"URL guard allowed {normalized}",
                    "confidence": 1.0,
                }
            ],
        )


class HttpFetch(BaseTool):
    name = "http_fetch"
    scene = "web_analysis"
    risk_level = RiskLevel.MEDIUM
    idempotent = True

    def __init__(
        self,
        guard: UrlGuard,
        transport=None,
        max_redirects: int = 3,
        max_body_bytes: int = 1_000_000,
    ) -> None:
        self.guard = guard
        self.transport = transport
        self.max_redirects = max_redirects
        self.max_body_bytes = max_body_bytes

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        current = params["url"]
        async with httpx.AsyncClient(
            transport=self.transport,
            follow_redirects=False,
            timeout=10.0,
        ) as client:
            for _ in range(self.max_redirects + 1):
                self.guard.check(current)
                response = await client.get(
                    current,
                    headers={"User-Agent": "SecAgent-X/0.1 Passive Analyzer"},
                )
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise BlockedUrl("blocked redirect without location")
                    current = urljoin(current, location)
                    continue
                body = response.content[: self.max_body_bytes]
                headers = dict(response.headers)
                body_preview = body.decode(
                    response.encoding or "utf-8", errors="replace"
                )
                header_names = ", ".join(sorted(headers))
                observation = {
                    "final_url": current,
                    "status_code": response.status_code,
                    "headers": headers,
                    "body_preview": body_preview,
                }
                return ToolResult(
                    success=True,
                    summary=f"HTTP {response.status_code}",
                    evidence=[
                        {
                            "evidence_type": "http_observation",
                            "source": current,
                            "content": (
                                f"HTTP {response.status_code} {current}; "
                                f"headers={header_names}"
                            ),
                            "confidence": 1.0,
                            "metadata": observation,
                        }
                    ],
                    warnings=(
                        ["响应体已截断"]
                        if len(response.content) > len(body)
                        else []
                    ),
                )
        raise BlockedUrl("blocked redirect limit exceeded")


class HeaderCheck(BaseTool):
    name = "header_check"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        response = params["response"]
        headers = {key.lower(): value for key, value in response["headers"].items()}
        checks = {
            "content-security-policy": "CSP",
            "strict-transport-security": "HSTS",
            "x-content-type-options": "X-Content-Type-Options",
        }
        missing = [label for key, label in checks.items() if key not in headers]
        if (
            "x-frame-options" not in headers
            and "frame-ancestors" not in headers.get("content-security-policy", "")
        ):
            missing.append("frame policy")
        findings = [
            {
                "kind": "missing_security_header_observation",
                "header": label,
                "severity": "info",
            }
            for label in missing
        ]
        return ToolResult(
            success=True,
            summary=f"观察到 {len(missing)} 项缺失的安全响应头",
            findings=findings,
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": response["final_url"],
                    "content": f"Header observation: missing {label}",
                    "confidence": 1.0,
                }
                for label in missing
            ],
        )


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict] = []
        self.current: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.lower() == "form":
            self.current = {
                "action": values.get("action", ""),
                "method": (values.get("method") or "get").lower(),
                "inputs": [],
            }
            self.forms.append(self.current)
        elif tag.lower() == "input" and self.current is not None:
            if values.get("name"):
                self.current["inputs"].append(values["name"])

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form":
            self.current = None


class FormExtract(BaseTool):
    name = "form_extract"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        response = params["response"]
        parser = _FormParser()
        parser.feed(response["body_preview"])
        evidence = [
            {
                "evidence_type": "http_observation",
                "source": response["final_url"],
                "content": (
                    f"Passive form: action={form['action']} method={form['method']} "
                    f"inputs={','.join(form['inputs'])}"
                ),
                "confidence": 1.0,
            }
            for form in parser.forms
        ]
        return ToolResult(
            success=True,
            summary=f"被动提取 {len(parser.forms)} 个表单（未提交）",
            findings=parser.forms,
            evidence=evidence,
        )
