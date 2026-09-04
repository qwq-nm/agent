from secagent.domain import TaskCreate
from secagent.db_models import EvidenceRow
from sqlalchemy import select


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


def test_record_evidence_reuses_duplicate_task_source_digest(repository, ledger) -> None:
    task = repository.create_task(
        TaskCreate(goal="分析网页", authorization_scope="仅测试数据")
    )

    first_id = ledger.record_evidence(
        task.id,
        evidence_type="http_observation",
        source="initial_recon:http://example.test/",
        content="标题：HTTP 头迷宫",
        confidence=0.92,
        metadata={"title": "HTTP 头迷宫"},
    )
    second_id = ledger.record_evidence(
        task.id,
        evidence_type="http_observation",
        source="initial_recon:http://example.test/",
        content="标题：HTTP 头迷宫",
        confidence=0.92,
        metadata={"title": "HTTP 头迷宫"},
    )

    rows = repository.session.scalars(
        select(EvidenceRow).where(EvidenceRow.task_id == task.id)
    ).all()
    assert second_id == first_id
    assert len(rows) == 1
