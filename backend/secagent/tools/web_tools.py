from html.parser import HTMLParser
from typing import Any

from secagent.domain import RiskLevel, ToolResult
from secagent.security.url_guard import UrlGuard
from secagent.tools.base import BaseTool, ToolContext
from secagent.tools.http_request import HttpRequest


class UrlGuardTool(BaseTool):
    name = "url_guard"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True

    def __init__(self, guard: UrlGuard) -> None:
        self.guard = guard

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        del context
        parsed = self.guard.check(params["url"])
        normalized = parsed.geturl()
        return ToolResult(
            success=True,
            summary="Target URL passed SSRF policy check",
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": normalized,
                    "content": f"URL guard allowed {normalized}",
                    "confidence": 1.0,
                }
            ],
        )


class HttpFetch(HttpRequest):
    """Backward-compatible name for the guarded HTTP request tool."""


def _response_param(params: dict[str, Any]) -> dict[str, Any] | ToolResult:
    response = params.get("response")
    if not isinstance(response, dict):
        return ToolResult(
            success=False,
            summary="HTTP response parameter is missing or invalid",
            error="invalid_http_response",
            warnings=[
                "Tool expected response metadata from http_fetch; got a different value."
            ],
        )
    return response


class HeaderCheck(BaseTool):
    name = "header_check"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        del context
        response = _response_param(params)
        if isinstance(response, ToolResult):
            return response
        raw_headers = response.get("headers")
        if not isinstance(raw_headers, dict):
            return ToolResult(
                success=False,
                summary="HTTP response headers are missing or invalid",
                error="invalid_http_response",
                warnings=[
                    "header_check requires the structured metadata produced by http_fetch."
                ],
            )
        headers = {str(key).lower(): str(value) for key, value in raw_headers.items()}
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
        source = str(response.get("final_url") or "unknown")
        return ToolResult(
            success=True,
            summary=f"Observed {len(missing)} missing security headers",
            findings=findings,
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": source,
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
        del context
        response = _response_param(params)
        if isinstance(response, ToolResult):
            return response
        body_preview = response.get("body_preview")
        if body_preview is None:
            body_preview = ""
        if not isinstance(body_preview, str):
            return ToolResult(
                success=False,
                summary="HTTP response body preview is invalid",
                error="invalid_http_response",
                warnings=[
                    "form_extract requires the structured metadata produced by http_fetch."
                ],
            )
        parser = _FormParser()
        parser.feed(body_preview)
        source = str(response.get("final_url") or "unknown")
        evidence = [
            {
                "evidence_type": "http_observation",
                "source": source,
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
            summary=f"Passively extracted {len(parser.forms)} forms",
            findings=parser.forms,
            evidence=evidence,
        )
