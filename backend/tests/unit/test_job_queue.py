import importlib
from pathlib import Path

import pytest


def _import(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"queue module is missing: {name}")


def test_fake_queue_deduplicates_by_command_id() -> None:
    module = _import("secagent.queue.fake")
    queue = module.FakeJobQueue()

    first = queue.enqueue("task-1", "command-1")
    second = queue.enqueue("task-1", "command-1")

    assert first == second
    assert len(queue.enqueued) == 1
    assert queue.enqueued[0].task_id == "task-1"
    assert queue.enqueued[0].command_id == "command-1"
    assert queue.enqueued[0].broker_id == first


def test_celery_uses_json_only_and_exact_worker_settings() -> None:
    module = _import("secagent.queue.celery_app")
    celery = module.celery

    assert celery.conf.task_serializer == "json"
    assert celery.conf.accept_content == ["json"]
    assert celery.conf.result_backend is None
    assert celery.conf.task_ignore_result is True
    assert celery.conf.task_acks_late is True
    assert celery.conf.worker_prefetch_multiplier == 1
    assert celery.conf.worker_concurrency == module.settings.worker_concurrency
    assert celery.conf.broker_connection_retry_on_startup is True


def test_celery_queue_sends_only_ids_and_reuses_command_as_task_id(
    monkeypatch,
) -> None:
    module = _import("secagent.queue.celery_queue")
    worker = _import("secagent.worker")
    calls = []

    class Result:
        id = "command-1"

    def apply_async(*, args, task_id):
        calls.append((args, task_id))
        return Result()

    monkeypatch.setattr(worker.run_task, "apply_async", apply_async)

    broker_id = module.CeleryJobQueue().enqueue("task-1", "command-1")

    assert broker_id == "command-1"
    assert calls == [(["task-1", "command-1"], "command-1")]


def test_compose_worker_loads_registered_task_module() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    compose = (repository_root / "docker-compose.yml").read_text(encoding="utf-8")

    assert '"secagent.worker"' in compose
