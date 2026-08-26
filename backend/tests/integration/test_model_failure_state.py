import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from secagent.agents.reporter import Reporter
from secagent.db_models import EvidenceRow, ModelCallRow, TaskRow, TaskStepRow
from secagent.domain import (
    ModelResponse,
    ModelStage,
    ParsedTask,
    RiskLevel,
    TaskCreate,
    TaskScene,
    TaskStatus,
)
from secagent.providers.base import (
    ProviderErrorCode,
    ProviderFailure,
    ProviderUnavailable,
)
from secagent.providers.mock import MockProvider
from secagent.providers.router import ModelRouter
from secagent.services.task_events import TaskEventService
from secagent.services.ledger import LedgerService
from secagent.services.job_service import JobService
from secagent.worker import execute_queued_task


class FailingProvider:
    name = "glm"

    async def complete(self, request):
        raise ProviderFailure(
            self.name,
            ProviderErrorCode.RATE_LIMIT,
            retryable=True,
            request_id="req-safe-123",
        )


class ForgedCitationRouter:
    def __init__(self, evidence_ids: str | list[str]) -> None:
        self.evidence_ids = (
            [evidence_ids] if isinstance(evidence_ids, str) else evidence_ids
        )

    async def complete(self, stage, request, preferred=None):
        del stage, request, preferred
        from secagent.domain import ModelResponse

        return ModelResponse(
            provider="glm",
            model="glm-safe",
            data={
                "summary": "summary",
                "findings": [],
                "recommendations": [],
                "uncertainties": [],
                "evidence_ids": self.evidence_ids,
            },
            latency_ms=1,
        )


def test_provider_failure_persists_only_sanitized_model_error(
    analyst_client, app, fake_queue
) -> None:
    app.state.model_router = ModelRouter(
        {"glm": FailingProvider(), "deepseek": MockProvider()}, mode="live"
    )
    task = analyst_client.post(
        "/api/tasks",
        json={"goal": "Inspect provider failure", "authorization_scope": "Owned data"},
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "provider-failure-001"},
    )
    job = fake_queue.enqueued[0]

    with pytest.raises(ProviderFailure):
        asyncio.run(
            execute_queued_task(
                job.task_id,
                job.command_id,
                app.state.session_factory,
                app.state.model_router,
                app.state.tool_registry,
                app.state.settings.data_dir,
            )
        )

    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert len(detail["model_calls"]) == 1
    failed = detail["model_calls"][0]
    assert failed["status"] == "error"
    assert failed["error_code"] == "rate_limit"
    assert failed["request_id"] == "req-safe-123"
    assert failed["attempt"] == 1
    assert "Authorization" not in str(failed)
    assert "response" not in str(failed)


def test_missing_fixed_provider_persists_safe_error_and_counts_call(
    analyst_client, app, fake_queue
) -> None:
    app.state.model_router = ModelRouter({"deepseek": MockProvider()}, mode="auto")
    task = analyst_client.post(
        "/api/tasks",
        json={"goal": "Missing GLM", "authorization_scope": "Owned data"},
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "missing-provider-001"},
    )
    job = fake_queue.enqueued[0]

    with pytest.raises(ProviderUnavailable):
        asyncio.run(
            execute_queued_task(
                job.task_id,
                job.command_id,
                app.state.session_factory,
                app.state.model_router,
                app.state.tool_registry,
                app.state.settings.data_dir,
            )
        )

    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["status"] == "failed_retryable"
    assert len(detail["model_calls"]) == 1
    failed = detail["model_calls"][0]
    assert failed == failed | {
        "provider": "glm",
        "model": "unknown",
        "stage": "task_parse",
        "status": "error",
        "error_code": "auth",
        "request_id": None,
        "attempt": 1,
    }
    assert failed["prompt_tokens"] == failed["completion_tokens"] == 0
    assert "Authorization" not in str(failed)


@pytest.mark.parametrize(
    ("limit_field", "dimension"),
    [
        ("max_input_tokens", "input_tokens"),
        ("max_output_tokens", "output_tokens"),
    ],
)
def test_consumed_token_budget_stops_before_provider_call(
    analyst_client, app, fake_queue, limit_field, dimension
) -> None:
    provider = MockProvider()
    provider.complete = AsyncMock(wraps=provider.complete)
    app.state.model_router = ModelRouter({"mock": provider}, mode="mock")
    task = analyst_client.post(
        "/api/tasks",
        json={"goal": "No extra provider call", "authorization_scope": "Owned data"},
    ).json()
    with app.state.session_factory() as session:
        setattr(session.get(TaskRow, task["id"]), limit_field, 0)
        session.commit()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": f"{dimension}-zero"},
    )
    job = fake_queue.enqueued[0]

    asyncio.run(
        execute_queued_task(
            job.task_id,
            job.command_id,
            app.state.session_factory,
            app.state.model_router,
            app.state.tool_registry,
            app.state.settings.data_dir,
        )
    )

    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["status"] == "failed"
    assert detail["model_calls"] == []
    assert provider.complete.await_count == 0
    with app.state.session_factory() as session:
        events = TaskEventService(session).after(task["id"], 0)
        exhausted = [event for event in events if event.event_type == "task.budget_exhausted"]
        assert json.loads(exhausted[0].payload_json) == {"dimension": dimension}


def test_checkpoint_rejects_model_call_from_another_logical_attempt(
    repository, fake_queue
) -> None:
    task = repository.create_task(
        TaskCreate(goal="Attempt-bound checkpoint", authorization_scope="Owned data")
    )
    repository.set_task_status(task.id, TaskStatus.QUEUED)
    job = repository.add_job_run(task.id, "attempt-two", attempt=2)
    job.status = "queued"
    repository.commit()
    lease = JobService(repository, fake_queue).claim(
        task.id, job.command_id, "worker-attempt-two"
    )
    assert lease is not None and lease.attempt == 2
    stale_call_id = repository.add_model_call(
        task_id=task.id,
        provider="glm",
        model="glm-5.2",
        stage="task_parse",
        route_reason="fixed_stage",
        input_summary="task_parse structured request",
        status="completed",
        is_demo=False,
        attempt=1,
    )

    with pytest.raises(ValueError, match="successful model call"):
        repository.save_orchestration_checkpoint(
            task.id,
            lease=lease,
            fingerprint="f" * 64,
            stage="task_parse",
            data={"scene": "incident_response"},
            model_call_id=stale_call_id,
        )

    current_call = ModelResponse(
        provider="glm",
        model="glm-5.2",
        data={},
        latency_ms=1,
    )
    current_call_id = LedgerService(repository).record_model_response(
        task.id, ModelStage.TASK_PARSE, current_call, lease=lease
    )
    repository.save_orchestration_checkpoint(
        task.id,
        lease=lease,
        fingerprint="f" * 64,
        stage="task_parse",
        data={"scene": "incident_response"},
        model_call_id=current_call_id,
    )
    checkpoint = repository.load_orchestration_checkpoint(
        task.id, lease=lease, fingerprint="f" * 64
    )
    assert checkpoint["stages"]["task_parse"]["model_call_id"] == current_call_id
    assert repository.session.get(ModelCallRow, current_call_id).attempt == 2


def test_fenced_checkpoint_and_budget_reads_release_their_transactions(
    repository, fake_queue
) -> None:
    task = repository.create_task(
        TaskCreate(goal="Release fenced reads", authorization_scope="Owned data")
    )
    repository.set_task_status(task.id, TaskStatus.QUEUED)
    job = repository.add_job_run(task.id, "fenced-read-001")
    job.status = "queued"
    repository.commit()
    lease = JobService(repository, fake_queue).claim(
        task.id, job.command_id, "worker-fenced-read"
    )
    assert lease is not None

    repository.budget_state(
        task.id,
        lease=lease,
        timeout_seconds=30,
        now=datetime.now(timezone.utc),
    )
    assert repository.session.in_transaction() is False

    assert repository.load_orchestration_checkpoint(
        task.id, lease=lease, fingerprint="f" * 64
    ) == {}
    assert repository.session.in_transaction() is False


def test_budget_state_preserves_a_non_null_persisted_deadline(
    repository, fake_queue
) -> None:
    task = repository.create_task(
        TaskCreate(goal="Preserve deadline", authorization_scope="Owned data")
    )
    repository.set_task_status(task.id, TaskStatus.QUEUED)
    job = repository.add_job_run(task.id, "persisted-deadline-001")
    job.status = "queued"
    repository.commit()
    lease = JobService(repository, fake_queue).claim(
        task.id, job.command_id, "worker-persisted-deadline"
    )
    assert lease is not None
    persisted_deadline = datetime.now(timezone.utc) - timedelta(minutes=5)
    row = repository.session.get(TaskRow, task.id)
    row.budget_deadline_at = persisted_deadline
    repository.commit()

    state = repository.budget_state(
        task.id,
        lease=lease,
        timeout_seconds=30,
        now=datetime.now(timezone.utc),
    )

    assert state["deadline"] == persisted_deadline
    repository.session.refresh(row)
    stored_deadline = row.budget_deadline_at
    if stored_deadline.tzinfo is None:
        stored_deadline = stored_deadline.replace(tzinfo=timezone.utc)
    assert stored_deadline == persisted_deadline


def test_budget_exhaustion_is_a_terminal_atomic_event(
    analyst_client, app, fake_queue
) -> None:
    task = analyst_client.post(
        "/api/tasks",
        json={"goal": "Stay inside budget", "authorization_scope": "Owned data"},
    ).json()
    with app.state.session_factory() as session:
        row = session.get(TaskRow, task["id"])
        row.max_model_calls = 0
        session.commit()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "budget-zero-001"},
    )
    job = fake_queue.enqueued[0]

    asyncio.run(
        execute_queued_task(
            job.task_id,
            job.command_id,
            app.state.session_factory,
            app.state.model_router,
            app.state.tool_registry,
            app.state.settings.data_dir,
        )
    )

    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["status"] == "failed"
    assert detail["model_calls"] == []
    with app.state.session_factory() as session:
        events = TaskEventService(session).after(task["id"], 0)
        exhausted = [event for event in events if event.event_type == "task.budget_exhausted"]
        assert len(exhausted) == 1
        assert json.loads(exhausted[0].payload_json) == {"dimension": "model_calls"}


def test_persisted_deadline_fails_pending_approval_step_atomically(
    analyst_client, app, fake_queue
) -> None:
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Passive web deadline",
            "authorization_scope": "GET only",
            "scene_hint": "web_analysis",
            "target_url": "http://web-demo/",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "deadline-first"},
    )
    first = fake_queue.enqueued[0]
    asyncio.run(
        execute_queued_task(
            first.task_id,
            first.command_id,
            app.state.session_factory,
            app.state.model_router,
            app.state.tool_registry,
            app.state.settings.data_dir,
        )
    )
    approved = analyst_client.post(
        f"/api/tasks/{task['id']}/approve",
        headers={"Idempotency-Key": "deadline-approved"},
        json={"approved": True, "reason": "Authorized passive GET"},
    )
    assert approved.status_code == 202
    with app.state.session_factory() as session:
        row = session.get(TaskRow, task["id"])
        row.budget_deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    second = fake_queue.enqueued[1]

    asyncio.run(
        execute_queued_task(
            second.task_id,
            second.command_id,
            app.state.session_factory,
            app.state.model_router,
            app.state.tool_registry,
            app.state.settings.data_dir,
        )
    )

    with app.state.session_factory() as session:
        task_row = session.get(TaskRow, task["id"])
        pending_step = session.query(TaskStepRow).filter_by(
            task_id=task["id"], step_index=2
        ).one()
        events = TaskEventService(session).after(task["id"], 0)
        assert task_row.status == "failed"
        assert pending_step.status == "failed"
        assert json.loads(pending_step.result_json)["dimension"] == "deadline"
        assert [event.event_type for event in events].count(
            "task.budget_exhausted"
        ) == 1
    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["reports"]
    report = analyst_client.get(f"/api/tasks/{task['id']}/report").text
    assert "阶段性安全分析报告" in report
    assert "budget exhausted: deadline" in report


def test_report_rejects_foreign_evidence_id_and_repository_persists_exact_ids(
    repository,
) -> None:
    current = repository.create_task(
        TaskCreate(goal="Current evidence", authorization_scope="Owned data")
    )
    foreign = repository.create_task(
        TaskCreate(goal="Foreign evidence", authorization_scope="Other data")
    )
    current_id = LedgerService(repository).record_evidence(
        current.id,
        evidence_type="observation",
        source="current",
        content="current evidence",
        confidence=1.0,
    )
    foreign_id = LedgerService(repository).record_evidence(
        foreign.id,
        evidence_type="observation",
        source="foreign",
        content="foreign evidence",
        confidence=1.0,
    )
    parsed = ParsedTask(
        scene=TaskScene.INCIDENT_RESPONSE,
        goal=current.goal,
        authorization_scope=current.authorization_scope,
        risk_level=RiskLevel.LOW,
    )

    with pytest.raises(ValueError, match="evidence citation"):
        asyncio.run(
            Reporter(
                ForgedCitationRouter(foreign_id), LedgerService(repository)
            ).render(current.id, parsed)
        )

    report_id = repository.save_report(
        current.id,
        "safe report",
        is_demo=False,
        evidence_ids=[current_id],
    )
    report = next(
        row for row in repository.ledger_rows(current.id)["reports"] if row.id == report_id
    )
    assert json.loads(report.evidence_ids_json) == [current_id]
    with pytest.raises(ValueError, match="evidence citation"):
        repository.save_report(
            current.id,
            "forged report",
            is_demo=False,
            evidence_ids=[foreign_id],
        )


def test_report_rejects_duplicate_explicit_evidence_citations(repository) -> None:
    task = repository.create_task(
        TaskCreate(goal="Duplicate citation", authorization_scope="Owned data")
    )
    evidence_id = LedgerService(repository).record_evidence(
        task.id,
        evidence_type="observation",
        source="current",
        content="current evidence",
        confidence=1.0,
    )
    parsed = ParsedTask(
        scene=TaskScene.INCIDENT_RESPONSE,
        goal=task.goal,
        authorization_scope=task.authorization_scope,
        risk_level=RiskLevel.LOW,
    )

    with pytest.raises(ValueError, match="evidence citation"):
        asyncio.run(
            Reporter(
                ForgedCitationRouter([evidence_id, evidence_id]),
                LedgerService(repository),
            ).render(task.id, parsed)
        )


def test_report_empty_evidence_citations_default_to_all_current_evidence(
    repository,
) -> None:
    task = repository.create_task(
        TaskCreate(goal="Default citations", authorization_scope="Owned data")
    )
    evidence_ids = [
        LedgerService(repository).record_evidence(
            task.id,
            evidence_type="observation",
            source=f"current-{index}",
            content=f"evidence {index}",
            confidence=1.0,
        )
        for index in range(2)
    ]
    parsed = ParsedTask(
        scene=TaskScene.INCIDENT_RESPONSE,
        goal=task.goal,
        authorization_scope=task.authorization_scope,
        risk_level=RiskLevel.LOW,
    )

    artifact, _ = asyncio.run(
        Reporter(
            ForgedCitationRouter([]), LedgerService(repository)
        ).render(task.id, parsed)
    )

    assert artifact.evidence_ids == evidence_ids


def test_evidence_hash_binds_canonical_json_and_safe_file_bytes(
    repository, tmp_path
) -> None:
    task = repository.create_task(
        TaskCreate(goal="Hash evidence", authorization_scope="Owned upload")
    )
    data_dir = tmp_path / "data"
    upload_root = data_dir / "tasks" / task.id / "uploads"
    upload_root.mkdir(parents=True)
    artifact = upload_root / "artifact.bin"
    artifact.write_bytes(b"artifact-bytes")
    ledger = LedgerService(repository, data_dir=data_dir, evidence_file_max_bytes=32)

    evidence_id = ledger.record_evidence(
        task.id,
        evidence_type="observation",
        source="artifact",
        content={"z": 2, "a": 1},
        confidence=1.0,
        file_ref="artifact.bin",
    )

    row = repository.session.get(EvidenceRow, evidence_id)
    canonical = b'{"a":1,"z":2}'
    assert row.sha256 == hashlib.sha256(canonical + b"artifact-bytes").hexdigest()
    assert row.file_ref == "artifact.bin"

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError, match="upload root"):
        ledger.record_evidence(
            task.id,
            evidence_type="observation",
            source="outside",
            content={"a": 1},
            confidence=1.0,
            file_ref=str(outside),
        )

    too_large = upload_root / "large.bin"
    too_large.write_bytes(b"x" * 33)
    with pytest.raises(ValueError, match="size limit"):
        ledger.record_evidence(
            task.id,
            evidence_type="observation",
            source="large",
            content={"a": 1},
            confidence=1.0,
            file_ref="large.bin",
        )
