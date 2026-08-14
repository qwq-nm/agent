from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
import hashlib
from threading import Barrier, Event, Lock

from fastapi.testclient import TestClient

from secagent.domain import TaskStatus
from secagent.repository import TaskRepository


@dataclass
class RecordingQueue:
    calls: list[tuple[str, str]] = field(default_factory=list)
    failures_remaining: int = 0

    def enqueue(self, task_id: str, command_id: str) -> str:
        self.calls.append((task_id, command_id))
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise ConnectionError("broker unavailable")
        return f"broker-{command_id}"


class BlockingQueue:
    def __init__(self, *, fail_all: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.entered = Event()
        self.second_entered = Event()
        self.release = Event()
        self.fail_all = fail_all
        self._lock = Lock()

    def enqueue(self, task_id: str, command_id: str) -> str:
        with self._lock:
            call_number = len(self.calls) + 1
            self.calls.append((task_id, command_id))
        if call_number == 1:
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("test did not release publisher")
        else:
            self.second_entered.set()
        if self.fail_all:
            raise ConnectionError("broker unavailable")
        return f"broker-{command_id}"


def test_run_endpoint_enqueues_once(analyst_client, app, repository) -> None:
    queue = RecordingQueue()
    app.state.job_queue = queue
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Inspect authorized logs",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    headers = {"Idempotency-Key": "run-001"}

    first = analyst_client.post(f"/api/tasks/{task['id']}/run", headers=headers)
    second = analyst_client.post(f"/api/tasks/{task['id']}/run", headers=headers)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["status"] == "queued"
    assert second.json()["status"] == "queued"
    assert [task_id for task_id, _command_id in queue.calls] == [task["id"]]
    command_id = queue.calls[0][1]
    job = repository.get_job_run(command_id)
    assert job.status == "queued"
    assert job.broker_id == f"broker-{command_id}"
    run_events = [
        event
        for event in repository.list_audit_events()
        if event.action == "task.run" and event.resource_id == task["id"]
    ]
    assert [(event.outcome, event.resource_type) for event in run_events] == [
        ("queued", "task")
    ]


def test_run_requires_idempotency_key(analyst_client) -> None:
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Inspect authorized logs",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()

    response = analyst_client.post(f"/api/tasks/{task['id']}/run")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_broker_failure_is_retryable_with_same_key(
    analyst_client, app, repository
) -> None:
    queue = RecordingQueue(failures_remaining=1)
    app.state.job_queue = queue
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Inspect authorized logs",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    headers = {"Idempotency-Key": "run-retry-001"}

    failed = analyst_client.post(f"/api/tasks/{task['id']}/run", headers=headers)
    command_id = queue.calls[0][1]
    assert repository.get_task(task["id"]).status.value == "failed_retryable"
    assert repository.get_job_run(command_id).status == "enqueue_failed"
    retry = analyst_client.post(f"/api/tasks/{task['id']}/run", headers=headers)

    assert failed.status_code == 503
    assert retry.status_code == 202
    assert retry.json()["status"] == "queued"
    assert len(queue.calls) == 2
    assert queue.calls[1][1] == command_id
    assert repository.get_job_run(command_id).status == "queued"


def test_same_key_recovers_a_publish_interrupted_after_database_commit(
    analyst_client, app, repository
) -> None:
    queue = RecordingQueue()
    app.state.job_queue = queue
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Recover interrupted publish",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    key = "recover-publish-001"
    command_id = hashlib.sha256(
        f"secagent:{task['id']}:run:{key}".encode()
    ).hexdigest()
    repository.add_job_run(task["id"], command_id)
    repository.transition_task_status(
        task["id"], TaskStatus.CREATED, TaskStatus.QUEUED, commit=False
    )
    repository.commit()

    response = analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": key},
    )

    assert response.status_code == 202
    assert queue.calls == [(task["id"], command_id)]
    assert repository.get_job_run(command_id).status == "queued"


def test_concurrent_same_key_requests_wait_for_one_successful_publisher(
    analyst_client, app, repository
) -> None:
    queue = BlockingQueue()
    app.state.job_queue = queue
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Serialize duplicate publishers",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    headers = {"Idempotency-Key": "concurrent-publish-001"}

    authorization = analyst_client.headers["Authorization"]
    with TestClient(app) as first_client, TestClient(app) as second_client:
        first_client.headers["Authorization"] = authorization
        second_client.headers["Authorization"] = authorization
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                first_client.post, f"/api/tasks/{task['id']}/run", headers=headers
            )
            assert queue.entered.wait(timeout=5)
            second = executor.submit(
                second_client.post, f"/api/tasks/{task['id']}/run", headers=headers
            )
            published_concurrently = queue.second_entered.wait(timeout=0.2)
            queue.release.set()
            responses = [first.result(timeout=5), second.result(timeout=5)]

    assert published_concurrently is False
    assert [response.status_code for response in responses] == [202, 202]
    assert len(queue.calls) == 1
    command_id = queue.calls[0][1]
    assert repository.get_job_run(command_id).status == "queued"
    assert repository.get_task(task["id"]).status is TaskStatus.QUEUED


def test_concurrent_same_key_requests_never_return_202_when_all_publishes_fail(
    analyst_client, app, repository
) -> None:
    queue = BlockingQueue(fail_all=True)
    app.state.job_queue = queue
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Fail duplicate publishers consistently",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    headers = {"Idempotency-Key": "concurrent-failure-001"}

    authorization = analyst_client.headers["Authorization"]
    with TestClient(app) as first_client, TestClient(app) as second_client:
        first_client.headers["Authorization"] = authorization
        second_client.headers["Authorization"] = authorization
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                first_client.post, f"/api/tasks/{task['id']}/run", headers=headers
            )
            assert queue.entered.wait(timeout=5)
            second = executor.submit(
                second_client.post, f"/api/tasks/{task['id']}/run", headers=headers
            )
            published_concurrently = queue.second_entered.wait(timeout=0.2)
            queue.release.set()
            responses = [first.result(timeout=5), second.result(timeout=5)]

    assert published_concurrently is False
    assert [response.status_code for response in responses] == [503, 503]
    assert len(queue.calls) == 2
    command_id = queue.calls[0][1]
    assert repository.get_job_run(command_id).status == "enqueue_failed"
    assert repository.get_task(task["id"]).status is TaskStatus.FAILED_RETRYABLE


def test_unique_insert_loser_waits_for_failed_publisher_and_retries(
    analyst_client, app, monkeypatch
) -> None:
    queue = RecordingQueue(failures_remaining=2)
    app.state.job_queue = queue
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Resolve concurrent command insert",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    insert_barrier = Barrier(2)
    original_add = TaskRepository.add_job_run

    def add_together(repository, task_id, command_id):
        insert_barrier.wait(timeout=5)
        return original_add(repository, task_id, command_id)

    monkeypatch.setattr(TaskRepository, "add_job_run", add_together)
    headers = {"Idempotency-Key": "unique-race-001"}
    authorization = analyst_client.headers["Authorization"]
    with TestClient(app) as first_client, TestClient(app) as second_client:
        first_client.headers["Authorization"] = authorization
        second_client.headers["Authorization"] = authorization
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = [
                future.result(timeout=10)
                for future in (
                    executor.submit(
                        first_client.post,
                        f"/api/tasks/{task['id']}/run",
                        headers=headers,
                    ),
                    executor.submit(
                        second_client.post,
                        f"/api/tasks/{task['id']}/run",
                        headers=headers,
                    ),
                )
            ]

    assert [response.status_code for response in responses] == [503, 503]
    assert len(queue.calls) == 2
