"""Conversation-scoped runtime memory for web/CTF autonomous solving.

Extracts a compact, redacted summary of "what was visited / discovered /
failed / flagged" from the conversation's evidence and tool-call rows, so the
next decompose / subtask prompt can plan against memory instead of repeating
already-tried actions.

This is the DAG-flow counterpart to ``LedgerService._runtime_memory`` (which
serves the legacy task flow). Both produce the same field names so the frontend
``RuntimeMemoryPanel`` and planner prompts can consume them uniformly.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from secagent.db_models import (
    ConversationEventRow,
    ConversationTurnRow,
    EvidenceRow,
    ToolCallRow,
)

_URLISH = re.compile(r"https?://[^\s'\"<>]+")
_FLAGISH = re.compile(r"(?i)\b(?:flag|ctf|nssctf|iscc|secagent)\{[^{}\s]{3,120}\}")

#: List-valued keys keep their order; ``last_new_evidence_at`` is a scalar.
_LIST_KEYS = (
    "visited_urls",
    "queued_urls",
    "discovered_links",
    "forms",
    "parameters",
    "cookies",
    "js_files",
    "api_endpoints",
    "robots_paths",
    "sensitive_paths",
    "candidate_flags",
    "interesting_findings",
    "failed_attempts",
    "blocked_actions",
    "tool_result_summary",
)

_SENSITIVE_MARKERS = (
    "flag",
    "admin",
    "debug",
    "upload",
    "backup",
    ".bak",
    ".zip",
    ".sql",
    ".env",
    ".git",
    "robots.txt",
    "secret",
    "token",
)


def _empty_memory() -> dict[str, Any]:
    memory: dict[str, Any] = {key: [] for key in _LIST_KEYS}
    memory["last_new_evidence_at"] = None
    return memory


def _append_unique(
    target: list[Any],
    value: Any,
    *,
    key: Any | None = None,
    limit: int = 100,
) -> None:
    if value in (None, "") or len(target) >= limit:
        return
    marker = key(value) if callable(key) else str(value)
    if any((key(item) if callable(key) else str(item)) == marker for item in target):
        return
    target.append(value)


def _looks_sensitive(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in _SENSITIVE_MARKERS)


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _list_of_dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


class RuntimeMemoryBuilder:
    """Accumulate memory from normalized evidence/tool-call dicts."""

    def __init__(self) -> None:
        self.memory = _empty_memory()
        self._visited: set[str] = set()

    def add_evidence(self, item: dict[str, Any]) -> None:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        content = str(item.get("content") or "")

        url = metadata.get("url") or metadata.get("final_url") or item.get("source")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            self._record_url(url[:512])

        for match in _URLISH.findall(content):
            self._record_url(match[:512])

        for match in _FLAGISH.findall(content):
            _append_unique(self.memory["candidate_flags"], match[:240], limit=30)

        self._collect_metadata(metadata)
        self._collect_content_hints(content)

        created = item.get("created_at")
        if created:
            self.memory["last_new_evidence_at"] = str(created)

    def add_tool_call(self, item: dict[str, Any]) -> None:
        tool_name = str(item.get("tool_name") or "")
        status = str(item.get("status") or "")
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        params = item.get("params") if isinstance(item.get("params"), dict) else {}

        summary = str(result.get("summary") or "").strip()
        success = bool(result.get("success"))
        error = str(result.get("error") or "")[:180] or None

        _append_unique(
            self.memory["tool_result_summary"],
            {
                "tool_name": tool_name,
                "status": status,
                "success": success,
                "summary": summary[:240],
                "error": error,
            },
            key=lambda it: f"{it['tool_name']}:{it['summary']}:{it['error']}",
            limit=60,
        )

        if status != "completed" or not success:
            _append_unique(
                self.memory["failed_attempts"],
                {
                    "tool_name": tool_name,
                    "summary": summary[:240] or "工具未成功返回有效结果",
                    "error": error or "未记录具体错误",
                },
                key=lambda it: f"{it['tool_name']}:{it['error']}",
                limit=40,
            )

        if tool_name in {"http_fetch", "http_request", "url_guard"}:
            for key in ("final_url", "url", "source"):
                value = result.get(key) or params.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    self._record_url(value[:512])

        for finding in _list_of_dicts(result.get("findings")):
            self._collect_metadata(finding)

        for evidence in _list_of_dicts(result.get("evidence")):
            meta = evidence.get("metadata")
            if isinstance(meta, dict):
                self._collect_metadata(meta)
            self._collect_content_hints(str(evidence.get("content") or ""))

    def add_blocked_action(self, tool_name: str, reason: str) -> None:
        _append_unique(
            self.memory["blocked_actions"],
            {
                "tool_name": str(tool_name)[:120],
                "reason": str(reason)[:240],
            },
            key=lambda it: f"{it['tool_name']}:{it['reason']}",
            limit=40,
        )

    def result(self) -> dict[str, Any]:
        memory = self.memory
        memory["queued_urls"] = [
            url for url in memory["queued_urls"] if url not in self._visited
        ][:80]
        return memory

    # ------------------------------------------------------------------ internals

    def _record_url(self, url: str) -> None:
        if not url:
            return
        _append_unique(self.memory["visited_urls"], url, limit=100)
        self._visited.add(url)

    def _collect_metadata(self, item: dict[str, Any]) -> None:
        url = item.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            target = url[:512]
            _append_unique(self.memory["discovered_links"], target, limit=120)
            if target not in self._visited:
                _append_unique(self.memory["queued_urls"], target, limit=120)
            if target.endswith(".js") or "/static/" in target or "/assets/" in target:
                _append_unique(self.memory["js_files"], target, limit=50)
            if "/api/" in target or target.rstrip("/").endswith("/api"):
                _append_unique(self.memory["api_endpoints"], target, limit=50)
            if _looks_sensitive(target):
                _append_unique(self.memory["sensitive_paths"], target, limit=50)

        value = item.get("value") or item.get("candidate") or item.get("raw")
        if isinstance(value, str) and _looks_sensitive(value[:512]):
            _append_unique(self.memory["sensitive_paths"], value[:512], limit=50)

        pattern = item.get("pattern")
        if isinstance(pattern, str):
            _append_unique(self.memory["candidate_flags"], pattern[:240], limit=30)

        keyword = item.get("keyword")
        if isinstance(keyword, str):
            _append_unique(
                self.memory["interesting_findings"],
                f"页面或脚本中出现关键词：{keyword[:80]}",
                limit=60,
            )

        if "missing_attribute" in item:
            _append_unique(
                self.memory["cookies"],
                f"Cookie 缺少安全属性：{str(item['missing_attribute'])[:80]}",
                limit=30,
            )

        if "action" in item or "inputs" in item:
            form = {
                "action": str(item.get("action") or "")[:240],
                "method": str(item.get("method") or "get")[:20],
                "inputs": item.get("inputs") if isinstance(item.get("inputs"), list) else [],
            }
            _append_unique(
                self.memory["forms"],
                form,
                key=lambda entry: f"{entry['method']}:{entry['action']}:{entry['inputs']}",
                limit=30,
            )
            for field in form["inputs"]:
                if isinstance(field, dict):
                    name = field.get("name")
                    if isinstance(name, str) and name:
                        _append_unique(self.memory["parameters"], name[:120], limit=80)

    def _collect_content_hints(self, content: str) -> None:
        for match in re.findall(r"Flag-like pattern observed:\s*([^\s]+)", content):
            _append_unique(self.memory["candidate_flags"], match[:240], limit=30)


def build_conversation_memory(
    session: Session, *, conversation_id: str
) -> dict[str, Any]:
    """Build the runtime memory for a conversation across all of its turns."""
    builder = RuntimeMemoryBuilder()

    turn_ids = session.scalars(
        select(ConversationTurnRow.id).where(
            ConversationTurnRow.conversation_id == conversation_id
        )
    ).all()
    if not turn_ids:
        return builder.result()

    evidence_rows = session.scalars(
        select(EvidenceRow)
        .where(EvidenceRow.turn_id.in_(turn_ids))
        .order_by(EvidenceRow.created_at.asc())
    ).all()
    for row in evidence_rows:
        builder.add_evidence(
            {
                "source": row.source,
                "content": row.content,
                "metadata": _json_object(row.metadata_json),
                "created_at": row.created_at.isoformat(),
            }
        )

    tool_rows = session.scalars(
        select(ToolCallRow)
        .where(ToolCallRow.turn_id.in_(turn_ids))
        .order_by(ToolCallRow.created_at.asc())
    ).all()
    for row in tool_rows:
        builder.add_tool_call(
            {
                "tool_name": row.tool_name,
                "status": row.status,
                "params": _json_object(row.params_json),
                "result": _json_object(row.result_json),
            }
        )

    blocked_rows = session.scalars(
        select(ConversationEventRow)
        .where(
            ConversationEventRow.conversation_id == conversation_id,
            ConversationEventRow.event_type == "subtask.tool.rejected",
        )
        .order_by(ConversationEventRow.created_at.asc())
    ).all()
    for row in blocked_rows:
        payload = _json_object(row.payload_json)
        builder.add_blocked_action(
            payload.get("tool_name", ""), payload.get("reason", "")
        )

    return builder.result()


__all__ = ["RuntimeMemoryBuilder", "build_conversation_memory"]
