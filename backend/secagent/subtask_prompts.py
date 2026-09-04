"""Prompt assembly for subtask execution.

The user payload is bounded, canonical JSON built from persisted backend
facts only: the subtask spec, dependency summaries, accumulated tool
evidence and the remaining budget. Raw prompts, credentials and hidden
reasoning never enter this payload.
"""

from __future__ import annotations

from typing import Any

from secagent.conversation_domain import canonical_json_dumps
from secagent.dag_domain import SubtaskRead, WORKER_RESPONSE_ADAPTER
from secagent.domain import ModelRequest

_SUBTASK_SYSTEM_PROMPT = (
    "You are an autonomous security solver. Solve the assigned objective by "
    "calling whitelisted tools and reading their full output, then deciding the "
    "next tool from what you learned. Return exactly one JSON object matching "
    "the supplied response schema. When you need external evidence, return "
    "status=tool_request with tool_name, params, reason, and expected_evidence; "
    "the tool result (including the page body) comes back to you. Keep going "
    "until you solve it — for a web challenge, if the page asks you to set an "
    "HTTP header, call http_fetch with that header (User-Agent/Cookie/Referer). "
    "Consult runtime_memory to avoid repeating visited URLs and failed attempts. "
    "Return status=completed with the answer (e.g. the flag) in summary only "
    "when it is actually found; otherwise keep requesting tools or return "
    "incomplete with the concrete next lead. Do not invent tool results."
)

_WORKER_RESPONSE_SCHEMA = {
    **WORKER_RESPONSE_ADAPTER.json_schema(),
    "title": "WorkerResponseDocument",
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
    runtime_memory: dict[str, Any] | None = None,
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
        "runtime_memory": runtime_memory or {},
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
        "output_contract": {
            "tool_request": {
                "status": "tool_request",
                "tool_name": "one item from subtask.allowed_tools",
                "params": "object matching the selected tool input",
                "reason": "why this call is needed for this subtask",
                "expected_evidence": "what evidence the call should add",
            },
            "subtask_result": {
                "status": "completed | incomplete | failed",
                "summary": "bounded conclusion for this subtask",
                "claims": "facts with evidence_ref, attachment_ref, or upstream_key",
                "evidence_refs": "tool evidence ids used by the result",
                "inference_notes": "separate assumptions from facts",
                "unresolved": "remaining gaps only when evidence is insufficient",
            },
        },
    }
    return ModelRequest(
        system=_SUBTASK_SYSTEM_PROMPT,
        user=canonical_json_dumps(payload),
        response_schema=_WORKER_RESPONSE_SCHEMA,
    )


__all__ = ["build_subtask_request"]
