from datetime import datetime, timedelta, timezone

from secagent.db_models import JobRunRow
from secagent.domain import TaskCreate, TaskStatus
from secagent.services.job_service import JobService
from secagent.services.task_events import TaskEventService
from secagent.domain import PlanStep, RiskLevel


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
    assert repository.session.get(__import__("secagent.db_models", fromlist=["TaskStepRow"]).TaskStepRow, step_id).attempt == 2

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
    assert repository.completed_step_result(task.id, key)["result"]["summary"] == "done"


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
