"""Prompt assembly for subtask execution.

The user payload is bounded, canonical JSON built from persisted backend
facts only: the subtask spec, dependency summaries, accumulated tool
evidence and the remaining budget. Raw prompts, credentials and hidden
reasoning never enter this payload.
"""

from __future__ import annotations

from typing import Any

from secagent.conversation_domain import canonical_json_dumps
from secagent.dag_domain import SubtaskRead
from secagent.domain import ModelRequest

_SUBTASK_SYSTEM_PROMPT = (
    "You are a bounded security-analysis subtask worker. Complete exactly the "
    "assigned subtask using the supplied dependency outputs and tool evidence. "
    "Return only the requested JSON document. When you need one whitelisted "
    "tool call instead, return a tool_request object."
)

_WORKER_RESPONSE_SCHEMA = {
    "title": "SubtaskResultDocument",
    "type": "object",
}


def build_subtask_request(
    *,
    subtask: SubtaskRead,
    goal_summary: str,
    dependency_outputs: list[dict[str, Any]],
    tool_observations: list[dict[str, Any]],
    remaining_model_calls: int,
    remaining_tool_calls: int,
    registered_tools: list[dict[str, Any]],
    preferred_provider: str,
) -> ModelRequest:
    payload = {
        "subtask": {
            "key": subtask.key,
            "title": subtask.title,
            "objective": subtask.objective,
            "allowed_tools": list(subtask.allowed_tools),
            "expected_output": subtask.expected_output,
            "assigned_provider": subtask.assigned_provider,
        },
        "turn_goal": goal_summary,
        "dependency_outputs": dependency_outputs,
        "tool_observations": tool_observations,
        "budget": {
            "remaining_model_calls": remaining_model_calls,
            "remaining_tool_calls": remaining_tool_calls,
        },
        "registered_tools": [
            {
                "name": tool.get("name"),
                "risk_level": tool.get("risk_level"),
                "description": tool.get("description", ""),
            }
            for tool in registered_tools
            if tool.get("name") in set(subtask.allowed_tools)
        ],
        "preferred": preferred_provider,
    }
    return ModelRequest(
        system=_SUBTASK_SYSTEM_PROMPT,
        user=canonical_json_dumps(payload),
        response_schema=_WORKER_RESPONSE_SCHEMA,
    )


__all__ = ["build_subtask_request"]
