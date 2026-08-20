from secagent.domain import TaskCreate


def test_ledger_persists_masked_model_tool_and_evidence_records(
    repository, ledger
) -> None:
    task = repository.create_task(
        TaskCreate(goal="分析日志", authorization_scope="仅上传文件")
    )
    ledger.record_model_call(
        task.id,
        provider="glm",
        stage="task_parse",
        route_reason="中文任务理解",
        input_summary="access.log",
        is_demo=False,
    )
    ledger.record_tool_call(
        task.id,
        tool_name="echo",
        params={"token": "sk-secret"},
        result={"summary": "ok"},
    )
    ledger.record_evidence(
        task.id,
        evidence_type="raw_line",
        source="access.log:1",
        content="GET /admin",
        confidence=0.9,
    )
    snapshot = ledger.snapshot(task.id)
    assert "sk-secret" not in str(snapshot)
    assert snapshot["evidences"][0]["source"] == "access.log:1"
