"""CTF/web autonomous solver loop: decide whether to keep planning.

This controller sits *outside* the DAG. The DAG executes one round
(decompose -> subtasks -> maybe synthesize); ``decide_continue`` reads the
conversation runtime memory, the incomplete subtasks, the latest evidence and
the accumulated failures, then returns either a more specific next-round goal
or a decision to stop (flag found / no leads / tools all blocked / stale).

Keeping the continue decision here (rather than inline in the scheduler) makes
the "run -> gave up -> bump -> continue" loop of the reference CTF agent
explicit: the DAG runs each round, and this loop owns the continue-vs-stop
call with memory so the model stops repeating already-tried actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from secagent.db_models import ConversationTurnRow, SubtaskRow
from secagent.services.runtime_memory import build_conversation_memory


@dataclass(frozen=True)
class ContinueDecision:
    should_continue: bool
    goal: str | None
    reason: str


def decide_continue(
    session: Session,
    *,
    turn: ConversationTurnRow,
    incomplete_required: list[SubtaskRow],
    evidence_items: list[dict[str, Any]],
    original_goal: str,
    max_stale_rounds: int = 2,
) -> ContinueDecision:
    """Return a continue decision for a turn whose required subtasks are incomplete.

    ``incomplete_required`` must be non-empty; the caller gates on web/CTF scene
    and "no existing replan turn" before invoking this.
    """
    memory = build_conversation_memory(
        session, conversation_id=turn.conversation_id
    )

    # Stop: every tool attempt was blocked or failed and nothing ran. A turn
    # whose only outcome is blocked_actions cannot make progress this round.
    if memory["blocked_actions"] and not memory["tool_result_summary"]:
        return ContinueDecision(False, None, "tools_all_blocked")

    # Stop: no new evidence for several consecutive rounds. The replan chain
    # tracks consecutive turns that produced no new leads; a stale chain means
    # the solver is stuck and should degrade to a partial result.
    if _stale_rounds(session, turn) >= max_stale_rounds:
        return ContinueDecision(False, None, "stale_no_new_evidence")

    goal = _build_continue_goal(
        original_goal, incomplete_required, evidence_items, memory
    )
    return ContinueDecision(True, goal, "promising_leads")


def _build_continue_goal(
    original_goal: str,
    incomplete_required: list[SubtaskRow],
    evidence_items: list[dict[str, Any]],
    memory: dict[str, Any],
) -> str:
    """Compose a next-round goal from memory so the model plans against facts.

    The goal names the concrete, not-yet-exhausted leads (unvisited URLs,
    candidate flags, forms, sensitive paths) and the already-failed attempts to
    avoid, instead of a generic "keep analysing".
    """
    unresolved = "、".join(item.title for item in incomplete_required[:4])
    parts: list[str] = [f"{original_goal}\n\n继续规划要求：上一轮仍未确认 flag。"]

    if unresolved:
        parts.append(f"未完成环节：{unresolved}。")

    if memory.get("candidate_flags"):
        parts.append(
            "已出现候选 flag：" + "、".join(str(f) for f in memory["candidate_flags"][:5])
            + "；请优先用最小被动步骤复核其来源，不要伪造 flag。"
        )

    queued = memory.get("queued_urls") or []
    if queued:
        parts.append("尚未访问的新 URL：" + "、".join(str(u) for u in queued[:6]) + "。")

    forms = memory.get("forms") or []
    if forms:
        actions = "、".join(
            str(f.get("action") or "当前页面") for f in forms[:4] if isinstance(f, dict)
        )
        if actions:
            parts.append(f"已发现的表单：{actions}。")

    sensitive = memory.get("sensitive_paths") or []
    if sensitive:
        parts.append("疑似敏感路径：" + "、".join(str(p) for p in sensitive[:6]) + "。")

    failed = memory.get("failed_attempts") or []
    if failed:
        tried = "、".join(
            str(f.get("tool_name") or "") for f in failed[:5] if isinstance(f, dict)
        )
        if tried:
            parts.append(f"已失败且不要重复的动作：{tried}。")

    blocked = memory.get("blocked_actions") or []
    if blocked:
        parts.append("部分主动动作已被安全策略阻止，改用被动、只读的替代工具。")

    lead_sources = "、".join(
        str(item.get("source", "")) for item in evidence_items[:3]
    )
    parts.append(
        f"已有线索来自 {lead_sources or '证据账本'}，请基于这些新证据重新拆解最小下一步，"
        "优先验证新路径、表单、Cookie、响应差异、错误信息或候选 flag。"
    )
    return "\n".join(parts)[:4000]


def _stale_rounds(session: Session, turn: ConversationTurnRow) -> int:
    """Count consecutive ancestor turns that produced no new leads.

    A turn counts as stale when it has no queued/discovered URLs and no
    candidate flags in its own turn evidence. Walking the replan chain lets us
    stop after ``max_stale_rounds`` consecutive empty rounds.
    """
    from secagent.db_models import EvidenceRow

    stale = 0
    current = session.get(ConversationTurnRow, turn.id)
    while current is not None and current.replan_from_turn_id:
        if not _turn_has_leads(session, current):
            stale += 1
        else:
            break
        current = session.get(ConversationTurnRow, current.replan_from_turn_id)
    return stale


def _turn_has_leads(session: Session, turn: ConversationTurnRow) -> bool:
    from secagent.db_models import EvidenceRow

    rows = session.scalars(
        select(EvidenceRow).where(EvidenceRow.turn_id == turn.id).limit(64)
    ).all()
    for row in rows:
        content = (row.content or "").lower()
        if any(
            token in content
            for token in (
                "flag",
                "candidate",
                "disallow",
                "set-cookie",
                "form",
                "login",
                "admin",
                "robots",
                "http://",
                "https://",
            )
        ):
            return True
        import json

        try:
            metadata = json.loads(row.metadata_json or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if isinstance(metadata, dict) and any(
            metadata.get(key) for key in ("url", "candidate_flags", "paths", "forms")
        ):
            return True
    return False


__all__ = ["ContinueDecision", "decide_continue"]
