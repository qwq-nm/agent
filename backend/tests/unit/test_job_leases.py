from datetime import datetime, timedelta, timezone

from secagent.db_models import JobRunRow, TaskRow
from secagent.domain import TaskCreate, TaskStatus
from secagent.services.job_service import JobService


def _queued_job(repository, command_id: str = "command-1"):
    task = repository.create_task(
        TaskCreate(goal="Inspect logs", authorization_scope="Uploaded logs only")
    )
    repository.set_task_status(task.id, TaskStatus.QUEUED)
    job = repository.add_job_run(task.id, command_id)
    job.status = "queued"
    repository.commit()
    return task, job


def test_claim_sets_durable_owner_attempt_and_lease(repository, fake_queue) -> None:
    task, job = _queued_job(repository)
    before_version = repository.session.get(TaskRow, task.id).status_version
    service = JobService(repository, fake_queue, lease_seconds=30)

    lease = service.claim(task.id, job.command_id, "worker-a")

    assert lease is not None
    assert lease.job_run_id == job.id
    assert lease.worker_id == "worker-a"
    assert lease.attempt == 1
    assert lease.lease_expires_at > datetime.now(timezone.utc)
    assert repository.get_task(task.id).status is TaskStatus.RUNNING
    assert repository.session.get(TaskRow, task.id).status_version == before_version + 1


def test_stale_worker_cannot_heartbeat_or_finish(repository, fake_queue) -> None:
    task, job = _queued_job(repository)
    service = JobService(repository, fake_queue, lease_seconds=30)
    lease = service.claim(task.id, job.command_id, "worker-a")
    assert lease is not None

    assert service.heartbeat(job.id, "worker-b") is None
    assert service.finish(job.id, "worker-b", "completed") is False

    persisted = repository.session.get(JobRunRow, job.id)
    assert persisted.status == "running"
    assert persisted.worker_id == "worker-a"


def test_expired_lease_cannot_finish(repository, fake_queue) -> None:
    task, job = _queued_job(repository)
    service = JobService(repository, fake_queue, lease_seconds=30)
    assert service.claim(task.id, job.command_id, "worker-a") is not None
    persisted = repository.session.get(JobRunRow, job.id)
    persisted.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    repository.commit()

    assert service.finish(job.id, "worker-a", "completed") is False


def test_expired_job_is_requeued_within_budget(repository, fake_queue) -> None:
    task, job = _queued_job(repository)
    service = JobService(
        repository, fake_queue, lease_seconds=30, max_auto_retries=1
    )
    lease = service.claim(task.id, job.command_id, "worker-a")
    assert lease is not None
    job = repository.session.get(JobRunRow, job.id)
    job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    repository.commit()

    recovered = service.recover_expired()

    assert recovered == 1
    assert fake_queue.enqueued[-1].task_id == task.id
    assert repository.get_task(task.id).status is TaskStatus.QUEUED
    assert repository.session.get(JobRunRow, job.id).attempt == 2


def test_owner_heartbeat_extends_active_lease_and_can_finish(
    repository, fake_queue
) -> None:
    task, job = _queued_job(repository)
    service = JobService(repository, fake_queue, lease_seconds=30)
    lease = service.claim(task.id, job.command_id, "worker-a")
    assert lease is not None

    renewed = service.heartbeat(job.id, "worker-a")

    assert renewed is not None
    assert renewed.lease_expires_at >= lease.lease_expires_at
    assert service.is_active(job.id, "worker-a") is True
    assert service.finish(job.id, "worker-a", "completed") is True
    assert service.is_active(job.id, "worker-a") is False
    assert repository.get_task(task.id).status is TaskStatus.COMPLETED


def test_failed_finish_sets_retryable_task_state(repository, fake_queue) -> None:
    task, job = _queued_job(repository)
    service = JobService(repository, fake_queue)
    assert service.claim(task.id, job.command_id, "worker-a") is not None

    assert service.finish(job.id, "worker-a", "failed") is True

    assert repository.get_task(task.id).status is TaskStatus.FAILED_RETRYABLE


def test_claim_rejects_duplicate_and_nonqueued_task(repository, fake_queue) -> None:
    task, job = _queued_job(repository)
    service = JobService(repository, fake_queue)
    assert service.claim(task.id, job.command_id, "worker-a") is not None
    assert service.claim(task.id, job.command_id, "worker-b") is None

    other_task, other_job = _queued_job(repository, "command-2")
    repository.set_task_status(other_task.id, TaskStatus.PAUSED)
    assert service.claim(other_task.id, other_job.command_id, "worker-c") is None
    assert repository.session.get(JobRunRow, other_job.id).status == "cancelled"


def test_finish_settles_pause_request(repository, fake_queue) -> None:
    task, job = _queued_job(repository)
    service = JobService(repository, fake_queue)
    assert service.claim(task.id, job.command_id, "worker-a") is not None
    repository.invalidate_jobs_for_transition(task.id, "pause")
    repository.commit()

    assert service.finish(job.id, "worker-a", "completed") is True
    assert repository.session.get(JobRunRow, job.id).status == "paused"
    assert repository.get_task(task.id).status is TaskStatus.PAUSED


def test_recovery_queue_failure_is_explicitly_retryable(repository) -> None:
    class FailingQueue:
        def enqueue(self, task_id: str, command_id: str) -> str:
            raise RuntimeError("broker unavailable")

    task, job = _queued_job(repository)
    service = JobService(repository, FailingQueue(), max_auto_retries=1)
    assert service.claim(task.id, job.command_id, "worker-a") is not None
    persisted = repository.session.get(JobRunRow, job.id)
    persisted.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    repository.commit()

    assert service.recover_expired() == 1
    assert repository.session.get(JobRunRow, job.id).status == "enqueue_failed"
    assert repository.get_task(task.id).status is TaskStatus.FAILED_RETRYABLE
