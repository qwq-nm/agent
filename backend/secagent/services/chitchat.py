"""Deterministic fast path for plain chitchat messages.

A short greeting with no attachments, no URL and no security-intent keywords
completes the turn immediately with a static reply instead of paying the full
DeepSeek decomposition pipeline (which alone takes 100+ seconds and produces
real subtasks for even a single "hi").
"""

from __future__ import annotations

import re

from secagent.agents.coordinator import DecompositionContext

_CHITCHAT_REPLY = (
    "你好!我是 SecAgent-X 安全分析助手,可以帮你做日志应急分析、"
    "静态源码审计或被动 Web 分析。请描述你的授权目标和要分析的内容~"
)

_GREETINGS = frozenset(
    {
        "hi",
        "hello",
        "hey",
        "yo",
        "hi there",
        "hello there",
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "你好呀",
        "嗨咯",
        "在吗",
        "在不在",
        "早上好",
        "下午好",
        "晚上好",
    }
)

# Any of these in the message means the user actually wants security work;
# the full pipeline must run. Keep this list conservative so genuine task
# messages never take the fast path.
_INTENT_KEYWORDS = (
    "分析",
    "审计",
    "扫描",
    "检测",
    "日志",
    "源码",
    "网站",
    "web",
    "url",
    "http",
    "端口",
    "漏洞",
    "攻击",
    "授权",
    "目标",
    "上传",
    "代码",
    "文件",
    "报告",
    "渗透",
    "注入",
    "xss",
    "sql",
    "flag",
    "ctf",
)

_MAX_CHITCHAT_LENGTH = 30


def quick_chitchat_reply(context: DecompositionContext) -> str | None:
    """Return a static reply when the turn is pure chitchat, else None."""
    if context.attachments:
        return None
    text = context.current_message.strip()
    if not text or len(text) > _MAX_CHITCHAT_LENGTH:
        return None
    lowered = text.lower()
    if "http://" in lowered or "https://" in lowered:
        return None
    if any(keyword in lowered for keyword in _INTENT_KEYWORDS):
        return None
    normalized = re.sub(r"[!！。.\s，,]+$", "", lowered)
    if normalized not in _GREETINGS:
        return None
    return _CHITCHAT_REPLY
