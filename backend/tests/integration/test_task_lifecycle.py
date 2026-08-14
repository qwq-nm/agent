from secagent.domain import PlanStep, RiskLevel, TaskStatus


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
