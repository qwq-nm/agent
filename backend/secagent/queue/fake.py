from uuid import uuid4

from secagent.queue.base import QueuedJob


class FakeJobQueue:
    def __init__(self) -> None:
        self.enqueued: list[QueuedJob] = []
        self._by_command: dict[str, QueuedJob] = {}

    def enqueue(self, task_id: str, command_id: str) -> str:
        existing = self._by_command.get(command_id)
        if existing is not None:
            return existing.broker_id
        job = QueuedJob(task_id, command_id, str(uuid4()))
        self._by_command[command_id] = job
        self.enqueued.append(job)
        return job.broker_id
