from typing import Any
from urllib.parse import urljoin, urlsplit

from secagent.domain import RiskLevel, ToolResult
from secagent.security.redaction import redact_text
from secagent.security.url_guard import UrlGuard
from secagent.tools.base import BaseTool, ToolContext


class BrowserSnapshot(BaseTool):
    name = "browser_snapshot"
    scene = "web_analysis"
    risk_level = RiskLevel.MEDIUM
    idempotent = False
    requires_human_confirm = True
    timeout_seconds = 90.0
    description = "使用无头浏览器加载授权页面，提取 JS 渲染后的可见文本、链接、表单和截图。"
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "wait_ms": {"type": "integer", "minimum": 500, "maximum": 8000},
        },
        "required": ["url"],
    }

    def __init__(self, guard: UrlGuard) -> None:
        self.guard = guard

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        url = self.guard.check(self._required_url(params.get("url"))).geturl()
        wait_ms = self._wait_ms(params.get("wait_ms"))
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return ToolResult(
                success=False,
                summary="浏览器快照工具未安装 Playwright，无法渲染页面",
                error="playwright_not_installed",
                warnings=[
                    "请重新构建包含 Playwright 和 Chromium 的 Docker 镜像。"
                ],
            )

        screenshot_dir = context.workspace / "browser-snapshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshot_dir / "snapshot.png"
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                page = await browser.new_page(
                    viewport={"width": 1366, "height": 900},
                    java_script_enabled=True,
                )
                async def route_handler(route: Any, request: Any) -> None:
                    await self._route_request(route, request, url)

                await page.route("**/*", route_handler)
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=25_000
                )
                await page.wait_for_timeout(wait_ms)
                try:
                    await page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass
                title = await page.title()
                visible_text = await page.locator("body").inner_text(timeout=5_000)
                links = await self._extract_links(page, url)
                forms = await self._extract_forms(page, url)
                await page.screenshot(path=str(screenshot_path), full_page=True)
                status_code = response.status if response is not None else None
            finally:
                await browser.close()

        text_preview = redact_text(visible_text, include_generic_key=True)[:4000]
        safe_url = redact_text(url, include_generic_key=True)
        findings = self._findings(links, forms, text_preview)
        metadata = {
            "final_url": safe_url,
            "status_code": status_code,
            "title": redact_text(title, include_generic_key=True)[:240],
            "text_preview": text_preview,
            "links": links[:80],
            "forms": forms[:30],
            "screenshot_path": str(screenshot_path),
        }
        evidence = [
            {
                "evidence_type": "http_observation",
                "source": safe_url,
                "content": (
                    f"浏览器渲染快照：标题“{metadata['title']}”，"
                    f"提取到 {len(links)} 个同源链接、{len(forms)} 个表单，"
                    f"截图已保存。"
                ),
                "confidence": 0.92,
                "metadata": metadata,
            }
        ]
        for item in findings:
            evidence.append(
                {
                    "evidence_type": "http_observation",
                    "source": str(item.get("url") or safe_url),
                    "content": str(item["content"]),
                    "confidence": 0.86,
                    "metadata": item,
                }
            )
        return ToolResult(
            success=True,
            summary=f"浏览器快照完成，提取 {len(links)} 个链接、{len(forms)} 个表单",
            findings=findings,
            evidence=evidence,
            metrics={
                "engine": "playwright_chromium",
                "status_code": status_code,
                "screenshot_path": str(screenshot_path),
            },
        )

    async def _route_request(self, route: Any, request: Any, base_url: str) -> None:
        method = str(request.method).upper()
        target = request.url
        if method not in {"GET", "HEAD", "OPTIONS"}:
            await route.abort()
            return
        if not self._same_origin(base_url, target):
            await route.abort()
            return
        try:
            self.guard.check(target)
        except Exception:
            await route.abort()
            return
        await route.continue_()

    @staticmethod
    async def _extract_links(page: Any, base_url: str) -> list[dict[str, str]]:
        raw_links = await page.eval_on_selector_all(
            "a[href], link[href], script[src]",
            """nodes => nodes.map(node => ({
                tag: node.tagName.toLowerCase(),
                attr: node.hasAttribute('href') ? 'href' : 'src',
                value: node.getAttribute(node.hasAttribute('href') ? 'href' : 'src')
            }))""",
        )
        links: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw_links:
            raw = str(item.get("value") or "").strip()
            if not raw:
                continue
            normalized = urljoin(base_url, raw)
            if not BrowserSnapshot._same_origin(base_url, normalized):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            links.append(
                {
                    "url": redact_text(normalized, include_generic_key=True),
                    "tag": str(item.get("tag") or ""),
                    "attr": str(item.get("attr") or ""),
                    "reason": BrowserSnapshot._reason(normalized),
                }
            )
        return links

    @staticmethod
    async def _extract_forms(page: Any, base_url: str) -> list[dict[str, Any]]:
        raw_forms = await page.eval_on_selector_all(
            "form",
            """forms => forms.map(form => ({
                action: form.getAttribute('action') || '',
                method: (form.getAttribute('method') || 'get').toLowerCase(),
                inputs: Array.from(form.querySelectorAll('input, textarea, select'))
                    .map(input => input.getAttribute('name') || input.getAttribute('id') || '')
                    .filter(Boolean)
            }))""",
        )
        forms: list[dict[str, Any]] = []
        for item in raw_forms:
            action = str(item.get("action") or "")
            forms.append(
                {
                    "action": redact_text(urljoin(base_url, action), include_generic_key=True)
                    if action
                    else "",
                    "method": str(item.get("method") or "get")[:20],
                    "inputs": item.get("inputs") if isinstance(item.get("inputs"), list) else [],
                }
            )
        return forms

    @staticmethod
    def _findings(
        links: list[dict[str, str]], forms: list[dict[str, Any]], text: str
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for link in links:
            if link["reason"] != "公开链接":
                findings.append(
                    {
                        **link,
                        "content": f"浏览器渲染后发现可关注链接：{link['url']}（{link['reason']}）",
                    }
                )
        for form in forms:
            findings.append(
                {
                    **form,
                    "content": (
                        f"浏览器渲染后发现表单：action={form['action']}，"
                        f"method={form['method']}，inputs={','.join(map(str, form['inputs']))}"
                    ),
                }
            )
        lowered = text.lower()
        for keyword in ("flag", "admin", "debug", "token", "secret"):
            if keyword in lowered:
                findings.append(
                    {
                        "keyword": keyword,
                        "content": f"浏览器渲染文本中出现关键词：{keyword}",
                    }
                )
        return findings[:80]

    @staticmethod
    def _required_url(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("url is required")
        return value.strip()

    @staticmethod
    def _wait_ms(value: object) -> int:
        if isinstance(value, int) and not isinstance(value, bool):
            return max(500, min(value, 8000))
        return 2500

    @staticmethod
    def _same_origin(base_url: str, candidate: str) -> bool:
        base = urlsplit(base_url)
        parsed = urlsplit(candidate)
        return base.scheme == parsed.scheme and base.netloc == parsed.netloc

    @staticmethod
    def _reason(url: str) -> str:
        lowered = url.lower()
        for token in ("flag", "admin", "debug", "api", "backup", "login", "upload"):
            if token in lowered:
                return f"包含关键词 {token}"
        if lowered.endswith(".js"):
            return "前端脚本"
        return "公开链接"


__all__ = ["BrowserSnapshot"]
