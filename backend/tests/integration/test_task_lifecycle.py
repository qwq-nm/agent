import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from fastapi.testclient import TestClient

from secagent.db_models import TaskRow
from secagent.domain import PlanStep, RiskLevel, TaskStatus, ToolResult
from secagent.repository import TaskRepository
from secagent.tools.base import BaseTool, ToolContext


class BlockingDemoTool(BaseTool):
    name = "demo_evidence"
    scene = "incident_response"
    risk_level = RiskLevel.LOW
    idempotent = True
    timeout_seconds = 5.0

    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.calls = 0

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        self.calls += 1
        self.entered.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        return ToolResult(success=True, summary="controlled completion")


def test_task_status_boundary_read_refreshes_identity_mapped_row(
    analyst_client, app, repository
) -> None:
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Refresh worker status boundary",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    repository.set_task_status(task["id"], TaskStatus.RUNNING)

    with app.state.session_factory() as worker_session:
        held_row = worker_session.get(TaskRow, task["id"])
        worker_repository = TaskRepository(worker_session)
        repository.set_task_status(task["id"], TaskStatus.PAUSED)

        assert held_row.status == TaskStatus.RUNNING.value
        assert worker_repository.get_task(task["id"]).status is TaskStatus.PAUSED


def test_pause_resume_cancel_and_recover(
    analyst_client, repository, fake_queue
) -> None:
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "分析示例日志",
            "authorization_scope": "仅示例数据",
            "route_mode": "auto",
        },
    ).json()
    task_id = task["id"]
    assert analyst_client.post(f"/api/tasks/{task_id}/pause").json()["status"] == "paused"
    resumed = analyst_client.post(
        f"/api/tasks/{task_id}/resume",
        headers={"Idempotency-Key": "resume-001"},
    )
    assert resumed.status_code == 202
    assert resumed.json()["status"] == "queued"
    assert [job.task_id for job in fake_queue.enqueued] == [task_id]
    assert analyst_client.post(f"/api/tasks/{task_id}/cancel").json()["status"] == "cancelled"
    repository.set_task_status(task_id, TaskStatus.RUNNING)
    assert repository.recover_interrupted_tasks() == 1
    assert repository.get_task(task_id).status is TaskStatus.FAILED_RETRYABLE


def test_retry_transitions_to_queued_and_enqueues(
    analyst_client, repository, fake_queue
) -> None:
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Retry authorized analysis",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    repository.set_task_status(task["id"], TaskStatus.FAILED_RETRYABLE)

    response = analyst_client.post(
        f"/api/tasks/{task['id']}/retry",
        headers={"Idempotency-Key": "retry-001"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert [job.task_id for job in fake_queue.enqueued] == [task["id"]]


def test_cancelled_queued_task_is_not_resurrected_by_worker(
    analyst_client, repository, fake_queue, run_queued_job
) -> None:
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Cancel queued analysis",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "cancel-run-001"},
    )
    cancelled = analyst_client.post(f"/api/tasks/{task['id']}/cancel")

    run_queued_job()

    assert cancelled.json()["status"] == "cancelled"
    assert repository.get_task(task["id"]).status is TaskStatus.CANCELLED
    assert repository.get_job_run(fake_queue.enqueued[0].command_id).status == "cancelled"


def test_pause_cancels_old_command_before_resume_enqueues_replacement(
    analyst_client, repository, fake_queue, run_queued_job
) -> None:
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Pause queued analysis",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "pause-run-001"},
    )
    analyst_client.post(f"/api/tasks/{task['id']}/pause")
    resumed = analyst_client.post(
        f"/api/tasks/{task['id']}/resume",
        headers={"Idempotency-Key": "pause-resume-001"},
    )

    run_queued_job(0)

    assert resumed.json()["status"] == "queued"
    assert repository.get_task(task["id"]).status is TaskStatus.QUEUED
    assert repository.get_job_run(fake_queue.enqueued[0].command_id).status == "cancelled"
    assert repository.get_job_run(fake_queue.enqueued[1].command_id).status == "queued"


def test_running_pause_blocks_resume_until_old_worker_settles(
    analyst_client, app, repository, fake_queue, run_queued_job
) -> None:
    tool = BlockingDemoTool()
    app.state.tool_registry._tools[tool.name] = tool
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Pause running controlled analysis",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "running-pause-001"},
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(run_queued_job)
        assert tool.entered.wait(timeout=5)
        paused = analyst_client.post(f"/api/tasks/{task['id']}/pause")
        premature_resume = analyst_client.post(
            f"/api/tasks/{task['id']}/resume",
            headers={"Idempotency-Key": "premature-resume-001"},
        )
        tool.release.set()
        worker.result(timeout=5)

    old_job = repository.get_job_run(fake_queue.enqueued[0].command_id)
    assert paused.json()["status"] == "paused"
    assert premature_resume.status_code == 409
    assert repository.get_task(task["id"]).status is TaskStatus.PAUSED
    assert old_job.status == "paused"
    assert tool.calls == 1
    assert len(fake_queue.enqueued) == 1
    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["tool_calls"] == []
    assert [call["stage"] for call in detail["model_calls"]] == [
        "task_parse",
        "plan",
    ]
    assert detail["reports"] == []

    resumed = analyst_client.post(
        f"/api/tasks/{task['id']}/resume",
        headers={"Idempotency-Key": "settled-resume-001"},
    )
    assert resumed.status_code == 202
    assert len(fake_queue.enqueued) == 2


def test_running_cancel_is_not_resurrected_when_old_worker_finishes(
    analyst_client, app, repository, fake_queue, run_queued_job
) -> None:
    tool = BlockingDemoTool()
    app.state.tool_registry._tools[tool.name] = tool
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Cancel running controlled analysis",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    analyst_client.post(
        f"/api/tasks/{task['id']}/run",
        headers={"Idempotency-Key": "running-cancel-001"},
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(run_queued_job)
        assert tool.entered.wait(timeout=5)
        cancelled = analyst_client.post(f"/api/tasks/{task['id']}/cancel")
        tool.release.set()
        worker.result(timeout=5)

    assert cancelled.json()["status"] == "cancelled"
    assert repository.get_task(task["id"]).status is TaskStatus.CANCELLED
    assert repository.get_job_run(fake_queue.enqueued[0].command_id).status == "cancelled"
    assert tool.calls == 1
    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["tool_calls"] == []
    assert [call["stage"] for call in detail["model_calls"]] == [
        "task_parse",
        "plan",
    ]
    assert detail["reports"] == []


def _waiting_task(analyst_client, repository) -> dict:
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Approve authorized passive fetch",
            "authorization_scope": "GET only",
        },
    ).json()
    repository.set_task_status(task["id"], TaskStatus.RUNNING)
    step_id = repository.add_step(
        task["id"],
        1,
        PlanStep(
            name="Fetch",
            purpose="Passive fetch",
            tool_name="http_fetch",
            params={},
            risk_level=RiskLevel.MEDIUM,
            need_human_confirm=True,
        ),
    )
    repository.add_approval(
        task["id"],
        step_id=step_id,
        tool_name="http_fetch",
        risk_level="medium",
        params_summary="{}",
    )
    repository.set_task_status(task["id"], TaskStatus.WAITING_HUMAN)
    return task


def test_approval_rejection_cancels_without_enqueue(
    analyst_client, repository, fake_queue
) -> None:
    task = _waiting_task(analyst_client, repository)

    response = analyst_client.post(
        f"/api/tasks/{task['id']}/approve",
        json={"approved": False, "reason": "Not authorized"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert fake_queue.enqueued == []


def test_approval_acceptance_creates_new_queued_command(
    analyst_client, repository, fake_queue
) -> None:
    task = _waiting_task(analyst_client, repository)

    response = analyst_client.post(
        f"/api/tasks/{task['id']}/approve",
        headers={"Idempotency-Key": "approve-001"},
        json={"approved": True, "reason": "Authorized passive GET"},
    )
    duplicate = analyst_client.post(
        f"/api/tasks/{task['id']}/approve",
        headers={"Idempotency-Key": "approve-001"},
        json={"approved": True, "reason": "Authorized passive GET"},
    )

    assert response.status_code == 202
    assert duplicate.status_code == 202
    assert response.json()["status"] == "queued"
    assert duplicate.json()["status"] == "queued"
    assert [job.task_id for job in fake_queue.enqueued] == [task["id"]]


def test_concurrent_same_key_approval_acceptance_replays_winner(
    analyst_client, app, repository, fake_queue, monkeypatch
) -> None:
    task = _waiting_task(analyst_client, repository)
    decision_barrier = Barrier(2)
    original_decide = TaskRepository.decide_latest_approval

    def decide_together(repo, task_id, **kwargs):
        decision_barrier.wait(timeout=5)
        return original_decide(repo, task_id, **kwargs)

    monkeypatch.setattr(TaskRepository, "decide_latest_approval", decide_together)
    authorization = analyst_client.headers["Authorization"]
    headers = {"Idempotency-Key": "concurrent-approve-001"}
    payload = {"approved": True, "reason": "Authorized passive GET"}
    with TestClient(app) as first_client, TestClient(app) as second_client:
        first_client.headers["Authorization"] = authorization
        second_client.headers["Authorization"] = authorization
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = [
                future.result(timeout=10)
                for future in (
                    executor.submit(
                        first_client.post,
                        f"/api/tasks/{task['id']}/approve",
                        headers=headers,
                        json=payload,
                    ),
                    executor.submit(
                        second_client.post,
                        f"/api/tasks/{task['id']}/approve",
                        headers=headers,
                        json=payload,
                    ),
                )
            ]

    assert [response.status_code for response in responses] == [202, 202]
    assert len(fake_queue.enqueued) == 1


def test_retry_creates_a_new_logical_attempt(
    analyst_client, repository, fake_queue
) -> None:
    task = analyst_client.post(
        "/api/tasks",
        json={"goal": "Retry failed work", "authorization_scope": "Owned data"},
    ).json()
    first = repository.add_job_run(task["id"], "failed-attempt-1")
    first.status = "failed"
    repository.set_task_status(task["id"], TaskStatus.FAILED_RETRYABLE)

    response = analyst_client.post(
        f"/api/tasks/{task['id']}/retry",
        headers={"Idempotency-Key": "retry-attempt-2"},
    )

    assert response.status_code == 202
    queued = repository.get_job_run(fake_queue.enqueued[0].command_id)
    assert queued.attempt == 2
