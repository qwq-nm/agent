"""Unit tests for provider output validation, including discriminated unions."""

import json

import pytest

from secagent.dag_domain import WORKER_RESPONSE_ADAPTER
from secagent.providers.validation import OutputValidationError, validate_output


def _worker_schema() -> dict:
    return {**WORKER_RESPONSE_ADAPTER.json_schema(), "title": "WorkerResponseDocument"}


def test_validate_output_accepts_tool_request_branch() -> None:
    value = validate_output(
        json.dumps(
            {
                "status": "tool_request",
                "tool_name": "http_fetch",
                "params": {},
                "reason": "需要获取首页",
                "expected_evidence": "页面正文",
            }
        ),
        _worker_schema(),
    )
    assert value["status"] == "tool_request"


def test_validate_output_accepts_subtask_result_branch() -> None:
    value = validate_output(
        json.dumps(
            {
                "status": "completed",
                "summary": "完成",
                "claims": [],
                "evidence_refs": [],
                "inference_notes": [],
                "unresolved": [],
            }
        ),
        _worker_schema(),
    )
    assert value["status"] == "completed"


def test_validate_output_rejects_unknown_status() -> None:
    with pytest.raises(OutputValidationError):
        validate_output(json.dumps({"status": "nope", "foo": 1}), _worker_schema())
