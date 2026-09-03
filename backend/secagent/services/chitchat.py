"""Deterministic fast path for plain chitchat messages.

Recognised everyday phrases (greetings, meta questions and acknowledgements)
complete the turn immediately with a friendly static reply instead of paying the
full DeepSeek decomposition pipeline (which alone takes 100+ seconds and
produces real subtasks for even a single "hi"). Security work still runs through
the full pipeline whenever the message carries an explicit intent keyword, a
URL, attachments or an analyzer target.
"""

from __future__ import annotations

import re

from secagent.agents.coordinator import DecompositionContext

# Warm, general-purpose reply: friendly for everyday use, and still lets the
# user know the assistant can do authorised security analysis when asked.
_CHITCHAT_REPLY = (
    "你好呀！我是 SecAgent-X，一个既能陪你日常聊天、答疑解惑，也能做安全分析的助手。"
    "你直接说想做什么就行——闲聊、问问题都可以；"
    "如果是要对某个目标做日志分析、源码审计或 Web/CTF 安全分析，"
    "告诉我授权范围内的目标，我就会开工。今天有什么我可以帮你的吗？"
)

_GREETINGS = frozenset(
    {
        "hi",
        "hii",
        "hiii",
        "hello",
        "hey",
        "heyy",
        "hiya",
        "yo",
        "hi there",
        "hello there",
        "hey there",
        "good morning",
        "good afternoon",
        "good evening",
        "hello world",
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "哈哟",
        "你好呀",
        "您好呀",
        "嗨咯",
        "在吗",
        "在不在",
        "在么",
        "有人吗",
        "早上好",
        "上午好",
        "中午好",
        "下午好",
        "晚上好",
        "你好在吗",
        "你在吗",
    }
)

# Explicit everyday acknowledgements / meta questions that are never security
# tasks. They take the same fast path so they never dump a compliance block.
_SMALLTALK = frozenset(
    {
        "你是谁",
        "你是什么",
        "介绍一下你自己",
        "你能做什么",
        "你可以做什么",
        "你会什么",
        "你能干嘛",
        "谢谢",
        "多谢",
        "感谢",
        "thank you",
        "thanks",
        "thx",
        "再见",
        "拜拜",
        "bye",
        "晚安",
    }
)

# Any of these in the message means the user actually wants security work; the
# full pipeline must run. Keep this list conservative so genuine task messages
# never take the fast path.
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

# Capability / help questions: asking what the agent can do or which tools it
# can call. These are answered directly (no decomposition) so they never turn
# into model subtasks that loop without a final result.
_CAPABILITY_MARKERS = (
    "你能干什么",
    "你能干嘛",
    "你能做什么",
    "你可以做什么",
    "你能帮我做什么",
    "能干什么",
    "能做什么",
    "能干嘛",
    "会什么",
    "有什么工具",
    "能用什么工具",
    "能调用什么",
    "调用什么工具",
    "有哪些工具",
    "什么工具",
    "你的能力",
    "能做什么",
    "help",
)

_MAX_CHITCHAT_LENGTH = 30


def _capability_reply(context: DecompositionContext) -> str:
    tool_names = [tool.name for tool in context.available_tools]
    listed = "、".join(tool_names) if tool_names else "（暂无可用工具）"
    return (
        "我可以帮你做这些安全分析：日志应急分析、静态源码审计、被动 Web 分析、"
        "路径/目录与敏感文件发现、漏洞与注入探测等。\n\n"
        f"当前可调用的工具：{listed}。\n"
        "告诉我要分析的目标（并在授权范围内说明范围），我就会开工。"
    )


def _normalize(text: str) -> str:
    """Lowercase and drop punctuation/symbols/emoji, keeping words and spaces.

    ``Hi!``, ``你好～`` and ``hello  there`` all normalise to clean tokens so a
    set membership lookup is robust against stray punctuation and whitespace.
    """
    kept = re.sub(r"[^\w\s]", "", text.lower())
    return re.sub(r"\s+", " ", kept).strip()


def quick_chitchat_reply(context: DecompositionContext) -> str | None:
    """Return a static reply when the turn is pure chitchat, else None."""
    if context.attachments:
        return None
    text = context.current_message.strip()
    if not text:
        return None
    lowered = text.lower()
    if "http://" in lowered or "https://" in lowered:
        return None
    if any(keyword in lowered for keyword in _INTENT_KEYWORDS):
        return None
    normalized = _normalize(text)
    if normalized in _GREETINGS or normalized in _SMALLTALK:
        return _CHITCHAT_REPLY
    # Capability / help questions are answered directly with the tool list, even
    # when a bit longer, so they never get decomposed into looping subtasks.
    if any(marker in normalized for marker in _CAPABILITY_MARKERS):
        return _capability_reply(context)
    if len(text) > _MAX_CHITCHAT_LENGTH:
        return None
    return None
