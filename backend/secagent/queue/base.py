from dataclasses import dataclass
from typing import Protocol

from secagent.dag_domain import JobKind


class JobQueue(Protocol):
    def enqueue(self, task_id: str, command_id: str) -> str: ...


class DagJobQueue(Protocol):
    """Publishes one durable DAG job by kind and reference id."""

    def enqueue_dag_job(self, kind: JobKind, ref_id: str, command_id: str) -> str: ...


@dataclass(frozen=True)
class QueuedJob:
    task_id: str
    command_id: str
    broker_id: str


@dataclass(frozen=True)
class DagQueuedJob:
    kind: JobKind
    ref_id: str
    command_id: str
    broker_id: str
