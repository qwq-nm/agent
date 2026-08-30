"""DAG scheduling: dependency-ready dispatch with per-conversation slots.

The scheduler runs in one transaction guarded by a row lock on the
conversation row so two competing schedulers can never oversubscribe the
concurrency cap. Jobs are idempotent by unique command id; the synthesize job
uses a constant command id so at most one can ever exist per turn.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from secagent.dag_domain import (
    TERMINAL_SUBTASK_STATUSES,
    JobKind,
    SubtaskStatus,
    SubtaskTransitionError,
    subtask_execute_command_id,
    turn_synthesize_command_id,
)
from secagent.db_models import ConversationRow, ConversationTurnRow, SubtaskRow
from secagent.repositories.dag_repository import DagRepository
from secagent.repository import TaskRepository

if TYPE_CHECKING:  # pragma: no cover
    from secagent.queue.base import DagJobQueue

_DISPATCHABLE_TURN_STATES = frozenset(
    {"scheduling", "running", "waiting_tool_approval"}
)
_SYNTHESIZE_EXPECTED_TURN_STATES = frozenset(
    {"scheduling", "running", "waiting_tool_approval"}
)


@dataclass(frozen=True)
class _PendingPublish:
    kind: JobKind
    ref_id: str
    command_id: str
    task_id: str


class DagScheduler:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        queue: "DagJobQueue",
        max_parallel: int = 3,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        self.session_factory = session_factory
        self.queue = queue
        self.max_parallel = max_parallel

    def schedule_turn(self, turn_id: str) -> int:
        return self._dispatch(turn_id)

    def on_subtask_finished(self, subtask_id: str) -> int:
        with self.session_factory() as session:
            row = session.get(SubtaskRow, subtask_id)
            if row is None:
                raise KeyError(subtask_id)
            turn_id = row.turn_id
        return self._dispatch(turn_id)

    # ------------------------------------------------------------------ internals

    def _dispatch(self, turn_id: str) -> int:
        to_publish: list[_PendingPublish] = []
        with self.session_factory() as session:
            dag = DagRepository(session)
            task_repository = TaskRepository(session)
            turn = dag.require_turn(turn_id)
            if turn.status not in _DISPATCHABLE_TURN_STATES:
                return 0
            if turn.task_id is None:
                return 0
            # Serialize competing schedulers for this conversation.
            session.execute(
                select(ConversationRow.id)
                .where(ConversationRow.id == turn.conversation_id)
                .with_for_update()
            )
            turn = dag.require_turn(turn_id)
            if turn.status not in _DISPATCHABLE_TURN_STATES:
                return 0

            created = self._fill_slots(
                session, dag, task_repository, turn, to_publish
            )
            created += self._maybe_synthesize(
                session, dag, task_repository, turn, to_publish
            )
            session.commit()

        self._publish(to_publish)
        return created

    def _fill_slots(
        self,
        session: Session,
        dag: DagRepository,
        task_repository: TaskRepository,
        turn: ConversationTurnRow,
        to_publish: list[_PendingPublish],
    ) -> int:
        conversation_id = turn.conversation_id
        in_flight = dag.running_subtask_count(conversation_id)
        ready = dag.ready_subtasks(turn.id)
        free = self.max_parallel - in_flight
        created = 0
        for subtask in ready:
            if free <= 0:
                break
            dag.transition_subtask(
                subtask.id,
                SubtaskStatus.QUEUED,
                expected={SubtaskStatus.PENDING_DEPENDENCY},
                reason="dependency satisfied and slot available",
            )
            command_id = subtask_execute_command_id(subtask.id, 1)
            job_row = task_repository.add_job_run(
                turn.task_id, command_id
            )
            job_row.job_kind = JobKind.SUBTASK_EXECUTE.value
            job_row.turn_id = turn.id
            job_row.subtask_id = subtask.id
            session.flush()
            to_publish.append(
                _PendingPublish(
                    JobKind.SUBTASK_EXECUTE, subtask.id, command_id, turn.task_id
                )
            )
            free -= 1
            created += 1
        if created and turn.status == "scheduling":
            dag.mark_turn_state(turn.id, "running", expected={"scheduling"})
        return created

    def _maybe_synthesize(
        self,
        session: Session,
        dag: DagRepository,
        task_repository: TaskRepository,
        turn: ConversationTurnRow,
        to_publish: list[_PendingPublish],
    ) -> int:
        turn_id = turn.id
        subtasks = dag.list_subtasks(turn_id)
        if not subtasks:
            return 0
        required = [item for item in subtasks if item.required]
        if not required or not all(
            item.status in TERMINAL_SUBTASK_STATUSES for item in required
        ):
            return 0

        # Non-required stragglers that can no longer influence synthesis are
        # skipped; a straggler already handed to a worker finishes on its own.
        for item in subtasks:
            if item.required:
                continue
            if item.status in {
                SubtaskStatus.PENDING_DEPENDENCY,
                SubtaskStatus.QUEUED,
            }:
                with suppress(SubtaskTransitionError):
                    dag.transition_subtask(
                        item.id,
                        SubtaskStatus.SKIPPED,
                        expected={
                            SubtaskStatus.PENDING_DEPENDENCY,
                            SubtaskStatus.QUEUED,
                        },
                        reason="not required for synthesis",
                    )

        command_id = turn_synthesize_command_id(turn_id)
        if task_repository.get_job_run(command_id) is not None:
            return 0
        job_row = task_repository.add_job_run(
            turn.task_id, command_id
        )
        job_row.job_kind = JobKind.TURN_SYNTHESIZE.value
        job_row.turn_id = turn_id
        session.flush()
        dag.record_turn_event(
            turn_id,
            "turn.synthesis.started",
            {"turn_id": turn_id, "plan_version": turn.plan_version},
        )
        to_publish.append(
            _PendingPublish(
                JobKind.TURN_SYNTHESIZE,
                turn_id,
                command_id,
                turn.task_id,
            )
        )
        return 1

    def _publish(self, to_publish: list[_PendingPublish]) -> None:
        if not to_publish:
            return
        with self.session_factory() as session:
            task_repository = TaskRepository(session)
            for entry in to_publish:
                state = task_repository.begin_job_publish(entry.command_id)
                if state != "claimed":
                    continue
                try:
                    broker_id = self.queue.enqueue_dag_job(
                        entry.kind, entry.ref_id, entry.command_id
                    )
                except Exception:
                    task_repository.mark_job_enqueue_failed(
                        entry.task_id, entry.command_id
                    )
                    task_repository.commit()
                    continue
                task_repository.mark_job_enqueued(entry.command_id, broker_id)

