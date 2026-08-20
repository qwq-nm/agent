from dataclasses import dataclass
from typing import Protocol


class JobQueue(Protocol):
    def enqueue(self, task_id: str, command_id: str) -> str: ...


@dataclass(frozen=True)
class QueuedJob:
    task_id: str
    command_id: str
    broker_id: str
