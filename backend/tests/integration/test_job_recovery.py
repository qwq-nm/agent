import hashlib
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from secagent.db_models import ApprovalRow, JobRunRow, TaskStepRow
from secagent.domain import TaskCreate, TaskStatus
from secagent.services.job_service import JobService
from secagent.services.task_events import TaskEventService
from secagent.services.ledger import LedgerService
from secagent.repository import TaskRepository
from secagent.domain import PlanStep, RiskLevel
from secagent.domain import ToolResult
from secagent.tools.base import BaseTool, ToolContext
from secagent.worker import execute_queued_task


class BlockingEvidenceTool(BaseTool):
    name = "demo_evidence"
    scene = "incident_response"
    risk_level = RiskLevel.LOW
    idempotent = True
    timeout_seconds = 10.0

    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        self.entered.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        return ToolResult(
            success=True,
            summary="old worker result",
            evidence=[
                {
                    "evidence_type": "observation",
                    "source": "old-worker",
                    "content": "must not persist",
                    "confidence": 1.0,
                }
            ],
        )


class RepeatedFailureTool(BaseTool):
    name = "demo_evidence"
    scene = "incident_response"
    risk_level = RiskLevel.LOW
    idempotent = True

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        return ToolResult(
            success=False,
            summary="controlled repeated failure",
            error="token=retry-secret",
        )


def _expired_running_job(repository, *, attempt: int) -> tuple[str, str]:
    task = repository.create_task(
        TaskCreate(goal="Recover scan", authorization_scope="Uploaded logs only")
    )
    repository.set_task_status(task.id, TaskStatus.QUEUED)
    job = repository.add_job_run(task.id, f"recover-{attempt}")
    job.status = "queued"
    job.attempt = attempt
    repository.commit()
    lease = JobService(repository, None, lease_seconds=30).claim(
        task.id, job.command_id, "lost-worker"
    )
    assert lease is not None
    persisted = repository.session.get(JobRunRow, job.id)
    persisted.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    repository.commit()
    return task.id, job.id


def test_expired_job_over_budget_fails_and_emits_event(repository, fake_queue) -> None:
    task_id, job_id = _expired_running_job(repository, attempt=2)
    service = JobService(
        repository, fake_queue, lease_seconds=30, max_auto_retries=1
    )

    assert service.recover_expired() == 1

    assert repository.get_task(task_id).status is TaskStatus.FAILED
    assert repository.session.get(JobRunRow, job_id).status == "failed"
    events = TaskEventService(repository.session).after(task_id, 0)
    assert events[-1].event_type == "task.failed"
    audit = repository.latest_audit_event("task.recover")
    assert audit is not None
    assert audit.outcome == "failed"


def test_expired_pause_request_settles_without_requeue(repository, fake_queue) -> None:
    task_id, job_id = _expired_running_job(repository, attempt=1)
    repository.invalidate_jobs_for_transition(task_id, "pause")
    repository.set_task_status(task_id, TaskStatus.PAUSED)

    recovered = JobService(
        repository, fake_queue, lease_seconds=30, max_auto_retries=1
    ).recover_expired()

    assert recovered == 1
    assert repository.session.get(JobRunRow, job_id).status == "paused"
    assert repository.get_task(task_id).status is TaskStatus.PAUSED
    assert fake_queue.enqueued == []


def test_expired_running_job_does_not_resurrect_cancelled_task(
    repository, fake_queue
) -> None:
    task_id, job_id = _expired_running_job(repository, attempt=1)
    repository.set_task_status(task_id, TaskStatus.CANCELLED)

    assert JobService(repository, fake_queue).recover_expired() == 1

    assert repository.get_task(task_id).status is TaskStatus.CANCELLED
    assert repository.session.get(JobRunRow, job_id).status == "cancelled"
    assert fake_queue.enqueued == []


def test_step_is_reused_only_after_success_with_evidence_hash(repository) -> None:
    task = repository.create_task(
        TaskCreate(goal="Idempotent scan", authorization_scope="Uploaded source only")
    )
    step = PlanStep(
        name="Scan",
        purpose="Inspect source",
        tool_name="source_scan",
        params={"path": "src"},
        risk_level=RiskLevel.LOW,
    )
    key = repository.step_idempotency_key(task.id, 1, step)
    step_id = repository.add_step(task.id, 1, step, idempotency_key=key)

    assert repository.completed_step_result(task.id, key) is None
    retry_id = repository.add_step(task.id, 1, step, idempotency_key=key)
    assert retry_id == step_id
    assert repository.session.get(TaskStepRow, step_id).attempt == 2

    repository.complete_step(
        step_id,
        {"success": True, "summary": "done"},
        ["a" * 64],
    )
    assert repository.completed_step_result(task.id, key) is None
    repository.add_evidence(
        task_id=task.id,
        evidence_type="observation",
        source="unlinked",
        content="unlinked evidence",
        sha256="a" * 64,
        confidence=1.0,
    )
    assert repository.completed_step_result(task.id, key) is None
    tool_call_id = repository.add_tool_call(
        task_id=task.id,
        step_id=step_id,
        tool_name="source_scan",
        params_json="{}",
        result_json="{}",
        status="completed",
        attempt=2,
    )
    repository.add_evidence(
        task_id=task.id,
        tool_call_id=tool_call_id,
        evidence_type="observation",
        source="source_scan-linked",
        content="verified evidence",
        sha256="a" * 64,
        confidence=1.0,
    )
    assert repository.completed_step_result(task.id, key) is None

    content = "verified evidence"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    repository.complete_step(
        step_id,
        {"success": True, "summary": "done"},
        [content_hash],
    )
    repository.add_evidence(
        task_id=task.id,
        tool_call_id=tool_call_id,
        evidence_type="observation",
        source="verified-source",
        content=content,
        sha256=content_hash,
        confidence=1.0,
    )
    assert repository.completed_step_result(task.id, key)["result"]["summary"] == "done"


def test_reset_step_invalidates_prior_approval(repository) -> None:
    task = repository.create_task(
        TaskCreate(
            goal="Retry an authorized request",
            authorization_scope="Only the configured target host",
        )
    )
    first = PlanStep(
        name="Fetch first path",
        purpose="Collect an authorized observation",
        tool_name="http_fetch",
        params={"url": "https://target.test/first"},
        risk_level=RiskLevel.MEDIUM,
        need_human_confirm=True,
    )
    first_key = repository.step_idempotency_key(task.id, 1, first)
    step_id = repository.add_step(
        task.id, 1, first, idempotency_key=first_key
    )
    approval_id = repository.add_approval(
        task.id,
        step_id=step_id,
        tool_name=first.tool_name,
        risk_level=first.risk_level.value,
        params_summary='{"url": "https://target.test/first"}',
    )
    repository.decide_latest_approval(
        task.id, approved=True, reason="Approve only the first path"
    )
    assert repository.is_tool_approved(
        task.id, first.tool_name, step_id=step_id
    )

    changed = first.model_copy(
        update={
            "name": "Fetch second path",
            "params": {"url": "https://target.test/second"},
        }
    )
    changed_key = repository.step_idempotency_key(task.id, 1, changed)
    assert changed_key != first_key
    assert (
        repository.add_step(
            task.id, 1, changed, idempotency_key=changed_key
        )
        == step_id
    )

    assert not repository.is_tool_approved(
        task.id, changed.tool_name, step_id=step_id
    )
    assert repository.session.get(ApprovalRow, approval_id).status == "superseded"


def test_failed_step_retry_with_same_params_invalidates_prior_approval(
    repository,
) -> None:
    task = repository.create_task(
        TaskCreate(
            goal="Retry the same authorized request",
            authorization_scope="Only the configured target host",
        )
    )
    step = PlanStep(
        name="Fetch target",
        purpose="Collect an authorized observation",
        tool_name="http_fetch",
        params={"url": "https://target.test/"},
        risk_level=RiskLevel.MEDIUM,
        need_human_confirm=True,
    )
    key = repository.step_idempotency_key(task.id, 1, step)
    step_id = repository.add_step(task.id, 1, step, idempotency_key=key)
    approval_id = repository.add_approval(
        task.id,
        step_id=step_id,
        tool_name=step.tool_name,
        risk_level=step.risk_level.value,
        params_summary='{"url": "https://target.test/"}',
    )
    repository.decide_latest_approval(
        task.id, approved=True, reason="Approve one attempt"
    )
    repository.update_step(step_id, "failed")

    assert repository.add_step(task.id, 1, step, idempotency_key=key) == step_id
    assert not repository.is_tool_approved(
        task.id, step.tool_name, step_id=step_id
    )
    assert repository.session.get(ApprovalRow, approval_id).status == "superseded"


def test_old_step_attempt_evidence_cannot_complete_new_attempt(repository) -> None:
    task = repository.create_task(
        TaskCreate(goal="Attempt-bound scan", authorization_scope="Owned source")
    )
    step = PlanStep(
        name="Scan",
        purpose="Inspect",
        tool_name="source_scan",
        params={},
        risk_level=RiskLevel.LOW,
    )
    key = repository.step_idempotency_key(task.id, 1, step)
    step_id = repository.add_step(task.id, 1, step, idempotency_key=key)
    repository.add_step(task.id, 1, step, idempotency_key=key)
    content = "old attempt evidence"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    tool_call_id = repository.add_tool_call(
        task_id=task.id,
        step_id=step_id,
        tool_name="source_scan",
        params_json="{}",
        result_json="{}",
        status="completed",
        attempt=1,
    )
    repository.add_evidence(
        task_id=task.id,
        tool_call_id=tool_call_id,
        evidence_type="observation",
        source="old-attempt",
        content=content,
        sha256=content_hash,
        confidence=1.0,
    )
    repository.complete_step(
        step_id,
        {"success": True, "summary": "stale"},
        [content_hash],
    )

    assert repository.session.get(TaskStepRow, step_id).attempt == 2
    assert repository.completed_step_result(task.id, key) is None


def test_partial_same_attempt_result_is_completed_without_duplicate_evidence(
    repository, fake_queue
) -> None:
    task = repository.create_task(
        TaskCreate(goal="Resume partial result", authorization_scope="Owned source")
    )
    repository.set_task_status(task.id, TaskStatus.QUEUED)
    job = repository.add_job_run(task.id, "partial-command")
    job.status = "queued"
    repository.commit()
    lease = JobService(repository, fake_queue).claim(
        task.id, job.command_id, "worker-a"
    )
    assert lease is not None
    step = PlanStep(
        name="Scan",
        purpose="Inspect",
        tool_name="source_scan",
        params={},
        risk_level=RiskLevel.LOW,
    )
    key = repository.step_idempotency_key(task.id, 1, step)
    step_id = repository.add_step(task.id, 1, step, idempotency_key=key)
    result = ToolResult(
        success=True,
        summary="same observation",
        evidence=[
            {
                "evidence_type": "observation",
                "source": "partial-source",
                "content": "same evidence",
                "confidence": 1.0,
            }
        ],
    )
    ledger = LedgerService(repository)
    ledger.record_tool_result(task.id, step_id, step.tool_name, {}, result)

    ledger.record_step_result(
        lease,
        step_id=step_id,
        tool_name=step.tool_name,
        params={},
        result=result,
    )

    rows = repository.ledger_rows(task.id)
    assert len(rows["tool_calls"]) == 1
    assert len(rows["evidences"]) == 1
    assert repository.session.get(TaskStepRow, step_id).status == "success"
    assert repository.completed_step_result(task.id, key)["result"]["summary"] == (
        "same observation"
    )


def test_recovered_worker_fences_old_tool_result_writes(
    analyst_client, app, fake_queue
) -> None:
    tool = BlockingEvidenceTool()
    app.state.tool_registry._tools[tool.name] = tool
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Analyze controlled evidence",
            "authorization_scope": "Built-in evidence only",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "stale-write-fence"},
    )
    queued = fake_queue.enqueued[0]

    with ThreadPoolExecutor(max_workers=1) as executor:
        old_worker = executor.submit(
            asyncio.run,
            execute_queued_task(
                queued.task_id,
                queued.command_id,
                app.state.session_factory,
                app.state.model_router,
                app.state.tool_registry,
                app.state.settings.data_dir,
                worker_id="worker-old",
                heartbeat_seconds=3600,
            ),
        )
        assert tool.entered.wait(timeout=5)
        with app.state.session_factory() as session:
            repository = TaskRepository(session)
            job = repository.get_job_run(queued.command_id)
            job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            repository.commit()
            assert JobService(repository, fake_queue).recover_expired() == 1
            replacement = JobService(repository, fake_queue).claim(
                task["id"], queued.command_id, "worker-new"
            )
            assert replacement is not None
        tool.release.set()
        old_worker.result(timeout=10)

    with app.state.session_factory() as session:
        repository = TaskRepository(session)
        rows = repository.ledger_rows(task["id"])
        assert rows["steps"][0].status == "pending"
        assert rows["steps"][0].result_json is None
        assert rows["tool_calls"] == []
        assert rows["evidences"] == []
        assert rows["reports"] == []


def test_repeated_runtime_error_reuses_canonical_evidence_and_settles_retry(
    analyst_client, app, fake_queue, repository
) -> None:
    app.state.tool_registry._tools["demo_evidence"] = RepeatedFailureTool()
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Exercise a repeatable failure",
            "authorization_scope": "Built-in evidence only",
        },
    ).json()
    first = analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "runtime-error-first"},
    )
    assert first.status_code == 202

    first_job = fake_queue.enqueued[0]
    with pytest.raises(RuntimeError, match="retry-secret"):
        asyncio.run(
            execute_queued_task(
                first_job.task_id,
                first_job.command_id,
                app.state.session_factory,
                app.state.model_router,
                app.state.tool_registry,
                app.state.settings.data_dir,
                worker_id="error-worker-first",
            )
        )
    retry = analyst_client.post(
        f"/api/tasks/{task['id']}/retry",
        headers={"Idempotency-Key": "runtime-error-second"},
    )
    assert retry.status_code == 202

    second_job = fake_queue.enqueued[1]
    with pytest.raises(RuntimeError, match="retry-secret"):
        asyncio.run(
            execute_queued_task(
                second_job.task_id,
                second_job.command_id,
                app.state.session_factory,
                app.state.model_router,
                app.state.tool_registry,
                app.state.settings.data_dir,
                worker_id="error-worker-second",
            )
        )

    rows = repository.ledger_rows(task["id"])
    runtime_errors = [
        row for row in rows["evidences"] if row.evidence_type == "runtime_error"
    ]
    assert len(runtime_errors) == 1
    assert "retry-secret" not in runtime_errors[0].content
    assert repository.get_job_run(first_job.command_id).status == "failed"
    assert repository.get_job_run(second_job.command_id).status == "failed"
    assert repository.get_task(task["id"]).status is TaskStatus.FAILED_RETRYABLE
    events = TaskEventService(repository.session).after(task["id"], 0)
    assert [event.event_type for event in events].count("task.failed_retryable") == 2
    failures = [
        audit
        for audit in repository.list_audit_events()
        if audit.resource_id == task["id"]
        and audit.action == "task.execute"
        and audit.outcome == "failure"
    ]
    assert len(failures) == 2


def test_step_without_evidence_hash_is_not_reused(repository) -> None:
    task = repository.create_task(
        TaskCreate(goal="Do not reuse placeholder", authorization_scope="Owned data")
    )
    step = PlanStep(
        name="Scan",
        purpose="Inspect",
        tool_name="source_scan",
        params={},
        risk_level=RiskLevel.LOW,
    )
    key = repository.step_idempotency_key(task.id, 1, step)
    step_id = repository.add_step(task.id, 1, step, idempotency_key=key)
    repository.update_step(step_id, "success", result_json='{"result": {}}')

    assert repository.completed_step_result(task.id, key) is None
