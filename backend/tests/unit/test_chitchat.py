"""Unit coverage for the chitchat fast path.

Locks in the everyday-AI behaviour: greetings, meta questions and
acknowledgements complete the turn with a friendly static reply, while
security-intent messages, URLs and attachments must still fall through to the
full decomposition pipeline.
"""

from secagent.agents.coordinator import DecompositionContext
from secagent.services.chitchat import _CHITCHAT_REPLY, quick_chitchat_reply


def _ctx(message: str, *, with_attachment: bool = False) -> DecompositionContext:
    data = {
        "current_message": message,
        "conversation_summary": "",
        "recent_messages": [],
        "attachments": (
            [
                {
                    "original_name": "project.zip",
                    "relative_path": "uploads/project.zip",
                    "content_type": "application/zip",
                    "sha256": "a" * 64,
                    "scan_summary": "归档安全扫描通过。",
                }
            ]
            if with_attachment
            else []
        ),
        "settings": {
            "authorization_scope": "",
            "safety_mode": "conservative",
            "allowed_targets": [],
            "requested_parallelism": 2,
        },
        "completed_subtasks": [],
        "evidence": [],
        "unresolved_questions": [],
        "available_tools": [
            {
                "name": "source_scanner",
                "risk_level": "low",
                "description": "静态源码扫描。",
            }
        ],
        "budget": {
            "max_subtasks": 4,
            "max_model_calls_per_subtask": 2,
            "max_tool_calls_per_subtask": 2,
            "timeout_seconds": 180,
            "max_replans": 1,
            "max_context_tokens": 8_000,
        },
    }
    return DecompositionContext.model_validate(data)


def test_greeting_variants_fast_path() -> None:
    for greeting in ("hi", "Hi!", "HELLO", "hello  there", "你好", "你好呀", "在吗", "早上好"):
        assert (
            quick_chitchat_reply(_ctx(greeting)) == _CHITCHAT_REPLY
        ), f"expected {greeting!r} to be treated as chitchat"


def test_smalltalk_fast_path() -> None:
    for phrase in ("你是谁", "你能做什么", "谢谢", "再见", "bye", "thanks"):
        assert quick_chitchat_reply(_ctx(phrase)) == _CHITCHAT_REPLY


def test_intent_keyword_runs_full_pipeline() -> None:
    for message in ("hi 分析一下这个日志", "帮我扫描这个网站", "审计一下源码"):
        assert quick_chitchat_reply(_ctx(message)) is None


def test_url_or_attachment_falls_through() -> None:
    assert (
        quick_chitchat_reply(_ctx("http://node4.example:26128/secret.php")) is None
    )
    assert quick_chitchat_reply(_ctx("hi", with_attachment=True)) is None


def test_plain_short_message_is_not_forced_to_chitchat() -> None:
    # Regression guard: a short, keyword-free message that is not a recognised
    # greeting/small-talk phrase must still reach the pipeline (never silently
    # fast-pathed), so real task triggers keep decomposing.
    for message in ("调度测试", "运行子任务", "审批流程", "停止场景"):
        assert quick_chitchat_reply(_ctx(message)) is None
