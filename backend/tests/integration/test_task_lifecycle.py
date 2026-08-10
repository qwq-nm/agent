from secagent.domain import TaskStatus


def test_pause_resume_cancel_and_recover(client, repository) -> None:
    task = client.post(
        "/api/tasks",
        json={
            "goal": "分析示例日志",
            "authorization_scope": "仅示例数据",
            "route_mode": "auto",
        },
    ).json()
    task_id = task["id"]
    assert client.post(f"/api/tasks/{task_id}/pause").json()["status"] == "paused"
    assert client.post(f"/api/tasks/{task_id}/resume").status_code == 202
    assert client.post(f"/api/tasks/{task_id}/cancel").json()["status"] == "cancelled"
    repository.set_task_status(task_id, TaskStatus.RUNNING)
    assert repository.recover_interrupted_tasks() == 1
    assert repository.get_task(task_id).status is TaskStatus.FAILED_RETRYABLE
