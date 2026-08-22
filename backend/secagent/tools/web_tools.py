from html.parser import HTMLParser
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from secagent.domain import RiskLevel, ToolResult
from secagent.security.url_guard import UrlGuard
from secagent.tools.base import BaseTool, ToolContext
from secagent.tools.http_request import HttpRequest


FLAG_RE = re.compile(
    r"(?i)\b(?:flag|ctf|nssctf|iscc|secagent)\{[^{}\s]{3,120}\}"
)
URLISH_RE = re.compile(
    r"""(?ix)
    (?:
        https?://[^\s"'<>`]+
        |
        /[A-Za-z0-9._~!$&'()*+,;=:@%/-]{1,240}
    )
    """
)
JS_ROUTE_RE = re.compile(
    r"""(?ix)
    ["'`]
    (
        /(?:api|admin|auth|user|flag|backup|static|upload|download|debug)
        [A-Za-z0-9._~!$&'()*+,;=:@%/?#-]{0,240}
    )
    ["'`]
    """
)
SENSITIVE_PATH_MARKERS = (
    "admin",
    "api",
    "backup",
    "debug",
    "flag",
    "login",
    "robots.txt",
    "upload",
)


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


class _LinkParser(HTMLParser):
    ATTRS = {
        "a": ("href",),
        "area": ("href",),
        "base": ("href",),
        "form": ("action",),
        "iframe": ("src",),
        "img": ("src",),
        "link": ("href",),
        "script": ("src",),
        "source": ("src", "srcset"),
    }

    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        wanted = self.ATTRS.get(tag.lower())
        if not wanted:
            return
        values = dict(attrs)
        for name in wanted:
            raw = values.get(name)
            if raw:
                self.items.append({"tag": tag.lower(), "attr": name, "value": raw})


def _body_preview(response: dict[str, Any]) -> str | ToolResult:
    body_preview = response.get("body_preview", "")
    if not isinstance(body_preview, str):
        return ToolResult(
            success=False,
            summary="HTTP response body preview is invalid",
            error="invalid_http_response",
            warnings=["Tool requires the structured metadata produced by http_fetch."],
        )
    return body_preview


def _final_url(response: dict[str, Any]) -> str:
    return str(response.get("final_url") or "")


def _same_origin_or_relative(base_url: str, candidate: str) -> bool:
    parsed = urlsplit(candidate)
    if not parsed.scheme and not parsed.netloc:
        return True
    return urlsplit(base_url).netloc == parsed.netloc


def _normalize_url(base_url: str, raw: str) -> str | None:
    value = raw.strip()
    if not value or value.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    candidate = urljoin(base_url, value)
    if not _same_origin_or_relative(base_url, candidate):
        return None
    return candidate


def _interesting_reason(url: str) -> str:
    lowered = url.lower()
    matched = [item for item in SENSITIVE_PATH_MARKERS if item in lowered]
    if matched:
        return "interesting marker: " + ",".join(matched[:3])
    suffix = urlsplit(url).path.lower().rsplit(".", 1)[-1]
    if suffix in {"js", "json", "txt", "bak", "zip", "sql", "env"}:
        return f"interesting suffix: .{suffix}"
    return "public link"


class LinkExtract(BaseTool):
    name = "link_extract"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        del context
        response = _response_param(params)
        if isinstance(response, ToolResult):
            return response
        body = _body_preview(response)
        if isinstance(body, ToolResult):
            return body
        base_url = _final_url(response)
        parser = _LinkParser()
        parser.feed(body)
        seen: set[str] = set()
        findings: list[dict[str, str]] = []
        for item in parser.items:
            normalized = _normalize_url(base_url, item["value"])
            if normalized is None or normalized in seen:
                continue
            seen.add(normalized)
            findings.append(
                {
                    "url": normalized,
                    "tag": item["tag"],
                    "attr": item["attr"],
                    "reason": _interesting_reason(normalized),
                }
            )
        return ToolResult(
            success=True,
            summary=f"Extracted {len(findings)} same-origin public links",
            findings=findings,
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": base_url,
                    "content": f"Public link from {item['tag']}[{item['attr']}]: {item['url']} ({item['reason']})",
                    "confidence": 0.9,
                    "metadata": {"url": item["url"], "reason": item["reason"]},
                }
                for item in findings
            ],
        )


class RobotsAnalyzer(BaseTool):
    name = "robots_analyzer"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        del context
        base_url = str(params.get("base_url") or "")
        response = params.get("response")
        robots_url = urljoin(base_url, "/robots.txt") if base_url else ""
        if not isinstance(response, dict):
            return ToolResult(
                success=True,
                summary="Prepared robots.txt candidate URL",
                findings=[{"url": robots_url, "reason": "standard robots.txt path"}],
                evidence=[
                    {
                        "evidence_type": "http_observation",
                        "source": robots_url,
                        "content": f"robots.txt candidate: {robots_url}",
                        "confidence": 0.7,
                        "metadata": {"url": robots_url},
                    }
                ]
                if robots_url
                else [],
            )
        body = _body_preview(response)
        if isinstance(body, ToolResult):
            return body
        source = _final_url(response)
        directives: list[dict[str, str]] = []
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
            if key in {"allow", "disallow", "sitemap"} and value:
                directives.append(
                    {
                        "directive": key,
                        "value": value,
                        "url": urljoin(source or base_url, value)
                        if key != "sitemap"
                        else value,
                    }
                )
        return ToolResult(
            success=True,
            summary=f"Parsed {len(directives)} robots.txt directives",
            findings=directives or [{"url": robots_url, "reason": "robots.txt candidate"}],
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": source or robots_url,
                    "content": (
                        f"robots.txt {item['directive']}: {item['value']} -> {item['url']}"
                    ),
                    "confidence": 0.95,
                    "metadata": item,
                }
                for item in directives
            ],
        )


class JsAnalyzer(BaseTool):
    name = "js_analyzer"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        del context
        response = _response_param(params)
        if isinstance(response, ToolResult):
            return response
        body = _body_preview(response)
        if isinstance(body, ToolResult):
            return body
        source = _final_url(response)
        paths = []
        seen: set[str] = set()
        for match in JS_ROUTE_RE.finditer(body):
            normalized = _normalize_url(source, match.group(1))
            if normalized and normalized not in seen:
                seen.add(normalized)
                paths.append(
                    {"url": normalized, "reason": _interesting_reason(normalized)}
                )
        keywords = sorted(
            {word for word in ("flag", "token", "secret", "debug", "admin") if word in body.lower()}
        )
        findings = paths + [{"keyword": item, "reason": "keyword in client content"} for item in keywords]
        return ToolResult(
            success=True,
            summary=f"Found {len(paths)} client-side route hints and {len(keywords)} keywords",
            findings=findings,
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": source,
                    "content": f"Client-side route hint: {item['url']} ({item['reason']})",
                    "confidence": 0.85,
                    "metadata": item,
                }
                for item in paths
            ]
            + [
                {
                    "evidence_type": "http_observation",
                    "source": source,
                    "content": f"Client-side keyword observed: {item}",
                    "confidence": 0.75,
                    "metadata": {"keyword": item},
                }
                for item in keywords
            ],
        )


class PathNormalizer(BaseTool):
    name = "path_normalizer"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        del context
        base_url = str(params.get("base_url") or "")
        response = params.get("response")
        raw_paths = params.get("paths", [])
        candidates: list[str] = []
        if isinstance(raw_paths, list):
            candidates.extend(str(item) for item in raw_paths)
        if isinstance(response, dict):
            body = _body_preview(response)
            if isinstance(body, ToolResult):
                return body
            base_url = _final_url(response) or base_url
            candidates.extend(match.group(0) for match in URLISH_RE.finditer(body))
        seen: set[str] = set()
        findings = []
        for raw in candidates:
            normalized = _normalize_url(base_url, raw)
            if normalized is None or normalized in seen:
                continue
            seen.add(normalized)
            findings.append({"raw": raw, "url": normalized, "reason": _interesting_reason(normalized)})
        return ToolResult(
            success=True,
            summary=f"Normalized {len(findings)} same-origin candidate paths",
            findings=findings,
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": base_url,
                    "content": f"Candidate path normalized: {item['raw']} -> {item['url']} ({item['reason']})",
                    "confidence": 0.8,
                    "metadata": item,
                }
                for item in findings
            ],
        )


class FlagPatternDetector(BaseTool):
    name = "flag_pattern_detector"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        del context
        response = params.get("response")
        source = str(params.get("source") or "")
        text = str(params.get("text") or "")
        if isinstance(response, dict):
            source = _final_url(response) or source
            body = _body_preview(response)
            if isinstance(body, ToolResult):
                return body
            text += "\n" + body
        matches = sorted(set(match.group(0) for match in FLAG_RE.finditer(text)))
        return ToolResult(
            success=True,
            summary=f"Detected {len(matches)} flag-like patterns",
            findings=[{"pattern": item} for item in matches],
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": source or "provided_text",
                    "content": f"Flag-like pattern observed: {item}",
                    "confidence": 0.98,
                    "metadata": {"pattern": item},
                }
                for item in matches
            ],
        )


class CookieAnalyzer(BaseTool):
    name = "cookie_analyzer"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True
    description = "被动分析响应中的 Set-Cookie 安全属性，不修改会话、不提交请求。"

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        del context
        response = _response_param(params)
        if isinstance(response, ToolResult):
            return response
        headers = response.get("headers")
        if not isinstance(headers, dict):
            return ToolResult(
                success=False,
                summary="HTTP response headers are missing or invalid",
                error="invalid_http_response",
            )
        source = _final_url(response)
        raw_cookie = ""
        for key, value in headers.items():
            if str(key).lower() == "set-cookie":
                raw_cookie = str(value)
                break
        if not raw_cookie:
            return ToolResult(success=True, summary="No Set-Cookie header observed")
        lowered = raw_cookie.lower()
        findings = []
        for attr in ("httponly", "secure", "samesite"):
            if attr not in lowered:
                findings.append({"missing_attribute": attr})
        return ToolResult(
            success=True,
            summary=f"Analyzed Set-Cookie header; missing {len(findings)} recommended attributes",
            findings=findings,
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": source or "http_response",
                    "content": f"Cookie security attribute missing: {item['missing_attribute']}",
                    "confidence": 0.9,
                    "metadata": item,
                }
                for item in findings
            ],
        )


class SensitiveFileChecker(BaseTool):
    name = "sensitive_file_checker"
    scene = "web_analysis"
    risk_level = RiskLevel.LOW
    idempotent = True
    description = "从已获取页面和公开链接中被动识别疑似敏感文件、备份文件或泄露路径线索。"

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        del context
        response = params.get("response")
        source = str(params.get("source") or "")
        text = str(params.get("text") or "")
        if isinstance(response, dict):
            source = _final_url(response) or source
            body = _body_preview(response)
            if isinstance(body, ToolResult):
                return body
            text += "\n" + body
        patterns = [
            r"(?i)(?:^|[/'\"])(?:\.env|\.git|\.svn|\.DS_Store)(?:$|[/'\"])",
            r"(?i)[A-Za-z0-9_-]+\.(?:bak|backup|old|zip|tar|gz|sql|7z|rar)",
            r"(?i)(?:config|database|db|backup|dump)[A-Za-z0-9_.-]*\.(?:php|inc|sql|zip|bak|txt)",
        ]
        findings: list[dict[str, str]] = []
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                value = match.group(0).strip("'\"/")
                if value and value not in {item["candidate"] for item in findings}:
                    findings.append({"candidate": value[:240]})
        return ToolResult(
            success=True,
            summary=f"Detected {len(findings)} sensitive file or backup path hints",
            findings=findings,
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": source or "provided_text",
                    "content": f"Sensitive file hint observed: {item['candidate']}",
                    "confidence": 0.86,
                    "metadata": item,
                }
                for item in findings
            ],
        )
