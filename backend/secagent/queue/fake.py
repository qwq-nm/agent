from uuid import uuid4

from secagent.dag_domain import JobKind
from secagent.queue.base import DagQueuedJob, QueuedJob


class FakeJobQueue:
    def __init__(self) -> None:
        self.enqueued: list[QueuedJob] = []
        self._by_command: dict[str, QueuedJob] = {}
        self.dag_enqueued: list[DagQueuedJob] = []
        self._dag_by_command: dict[str, DagQueuedJob] = {}
        self.fail_next_enqueue: bool = False

    def enqueue(self, task_id: str, command_id: str) -> str:
        existing = self._by_command.get(command_id)
        if existing is not None:
            return existing.broker_id
        if self.fail_next_enqueue:
            self.fail_next_enqueue = False
            raise RuntimeError("broker unavailable")
        job = QueuedJob(task_id, command_id, str(uuid4()))
        self._by_command[command_id] = job
        self.enqueued.append(job)
        return job.broker_id

    def enqueue_dag_job(self, kind: JobKind, ref_id: str, command_id: str) -> str:
        existing = self._dag_by_command.get(command_id)
        if existing is not None:
            return existing.broker_id
        if self.fail_next_enqueue:
            self.fail_next_enqueue = False
            raise RuntimeError("broker unavailable")
        job = DagQueuedJob(kind, ref_id, command_id, str(uuid4()))
        self._dag_by_command[command_id] = job
        self.dag_enqueued.append(job)
        return job.broker_id
