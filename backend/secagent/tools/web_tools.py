from html.parser import HTMLParser

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


class HttpFetch(HttpRequest):
    """Backward-compatible name for the guarded HTTP request tool."""


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
