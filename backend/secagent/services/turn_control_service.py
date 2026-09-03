"""User-driven turn control: failure decisions, approvals, stop and replan.

Every path is an explicit user decision recorded on the failure/approval row,
paired with audit events. The system never decides on its own and never
falls back across providers.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from secagent.dag_domain import (
    JobKind,
    ModelFailureDecision,
    ModelFailureDecisionInput,
    ModelFailureRead,
    ModelFailureStage,
    SubtaskStatus,
    SubtaskTransitionError,
    subtask_execute_command_id,
    turn_decompose_command_id,
    turn_synthesize_command_id,
)
from secagent.conversation_domain import TurnBudgetSnapshot
from secagent.conversation_repository import ConversationRepository
from secagent.db_models import (
    ApprovalRow,
    ConversationTurnRow,
    JobRunRow,
    SubtaskRow,
)
from secagent.domain import TaskCreate
from secagent.repositories.dag_repository import DagRepository
from secagent.repository import TaskRepository

_ACTIVE_SUBTASK_STATES = {
    SubtaskStatus.PENDING_DEPENDENCY,
    SubtaskStatus.QUEUED,
    SubtaskStatus.RUNNING,
    SubtaskStatus.WAITING_TOOL_APPROVAL,
    SubtaskStatus.WAITING_MODEL_DECISION,
}


class UnknownModelFailure(KeyError):
    pass


class ModelFailureConflict(RuntimeError):
    pass


class UnknownApproval(KeyError):
    pass


class ApprovalConflict(RuntimeError):
    pass


class InvalidFailureDecision(RuntimeError):
    """The decision is not applicable to this failure's stage."""


class TurnControlService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        queue: Any,
        logical_providers: frozenset[str],
        max_parallel: int = 3,
        max_auto_continues: int = 15,
    ) -> None:
        self.session_factory = session_factory
        self.queue = queue
        self.logical_providers = logical_providers
        self.max_parallel = max_parallel
        self.max_auto_continues = max_auto_continues

    # ------------------------------------------------------------ failure decisions

    def resolve_failure(
        self,
        failure_id: str,
        decision: ModelFailureDecisionInput,
        actor_id: str,
    ) -> ModelFailureRead:
        to_publish: list[tuple[JobKind, str, str, str]] = []
        with self.session_factory() as session:
            dag = DagRepository(session)
            task_repository = TaskRepository(session)
            failure = dag.get_model_failure(failure_id)
            if failure is None:
                raise UnknownModelFailure(failure_id)
            if failure.status.value != "waiting_decision":
                raise ModelFailureConflict(failure_id)

            if decision.decision is ModelFailureDecision.RETRY_SAME:
                self._retry_same(session, dag, task_repository, failure, to_publish)
            elif decision.decision is ModelFailureDecision.REASSIGN:
                self._reassign(
                    session, dag, task_repository, failure, decision, to_publish
                )
            elif decision.decision is ModelFailureDecision.SKIP_AND_REPLAN:
                self._skip_and_replan(
                    session, dag, task_repository, failure, actor_id, to_publish
                )
            else:
                self._terminate_turn(
                    session, dag, task_repository, failure, actor_id, to_publish
                )

            resolved = dag.resolve_model_failure(
                failure_id,
                decision=decision.decision,
                decided_by=actor_id,
            )
            session.commit()

        self._publish(to_publish)
        return resolved

    def _retry_same(
        self,
        session: Session,
        dag: DagRepository,
        task_repository: TaskRepository,
        failure: ModelFailureRead,
        to_publish: list[tuple[JobKind, str, str, str]],
    ) -> None:
        turn = dag.require_turn(failure.turn_id)
        if failure.stage is ModelFailureStage.SUBTASK:
            assert failure.subtask_id is not None
            subtask = dag.require_subtask(failure.subtask_id)
            dag.transition_subtask(
                subtask.id,
                SubtaskStatus.QUEUED,
                expected={SubtaskStatus.WAITING_MODEL_DECISION},
                reason="user chose retry_same",
            )
            self._enqueue_subtask_retry(
                session, dag, task_repository, subtask, to_publish
            )
            dag.mark_turn_state(
                failure.turn_id,
                "running",
                expected={"waiting_model_decision"},
            )
        else:
            # Decompose/Synthesize retry: requeue the original failed job row.
            command_id = self._original_failed_command(session, failure.turn_id)
            if command_id is None or not dag.requeue_failed_dag_job(command_id):
                raise ModelFailureConflict(failure.turn_id)
            dag.mark_turn_state(
                failure.turn_id,
                "created" if failure.stage is ModelFailureStage.DECOMPOSE else "running",
                expected={"waiting_model_decision"},
            )
            to_publish.append(
                (
                    JobKind.TURN_DECOMPOSE
                    if failure.stage is ModelFailureStage.DECOMPOSE
                    else JobKind.TURN_SYNTHESIZE,
                    failure.turn_id,
                    command_id,
                    turn.task_id or "",
                )
            )

    def _reassign(
        self,
        session: Session,
        dag: DagRepository,
        task_repository: TaskRepository,
        failure: ModelFailureRead,
        decision: ModelFailureDecisionInput,
        to_publish: list[tuple[JobKind, str, str, str]],
    ) -> None:
        if failure.stage is not ModelFailureStage.SUBTASK:
            raise InvalidFailureDecision("reassign is only valid for subtasks")
        if decision.target_provider not in self.logical_providers:
            raise ModelFailureConflict(
                f"target provider {decision.target_provider} is not available"
            )
        assert failure.subtask_id is not None
        subtask = dag.require_subtask(failure.subtask_id)
        dag.set_assigned_provider(subtask.id, decision.target_provider or "glm")
        dag.transition_subtask(
            subtask.id,
            SubtaskStatus.QUEUED,
            expected={SubtaskStatus.WAITING_MODEL_DECISION},
            reason=f"user reassigned to {decision.target_provider}",
        )
        self._enqueue_subtask_retry(session, dag, task_repository, subtask, to_publish)
        dag.mark_turn_state(
            failure.turn_id,
            "running",
            expected={"waiting_model_decision"},
        )

    def _skip_and_replan(
        self,
        session: Session,
        dag: DagRepository,
        task_repository: TaskRepository,
        failure: ModelFailureRead,
        actor_id: str,
        to_publish: list[tuple[JobKind, str, str, str]],
    ) -> None:
        turn = dag.require_turn(failure.turn_id)
        if failure.stage is ModelFailureStage.SUBTASK:
            assert failure.subtask_id is not None
            try:
                dag.transition_subtask(
                    failure.subtask_id,
                    SubtaskStatus.SKIPPED,
                    expected=_ACTIVE_SUBTASK_STATES,
                    reason="user requested skip and replan",
                )
            except SubtaskTransitionError:
                pass
        dag.supersede_active_subtasks(
            failure.turn_id, "user requested skip and replan"
        )
        dag.mark_turn_state(
            failure.turn_id,
            "superseded",
            expected={
                "waiting_model_decision",
                "running",
                "scheduling",
                "waiting_tool_approval",
            },
        )
        self._create_replan_turn(
            session, dag, task_repository, turn, actor_id, to_publish
        )

    def _terminate_turn(
        self,
        session: Session,
        dag: DagRepository,
        task_repository: TaskRepository,
        failure: ModelFailureRead,
        actor_id: str,
        to_publish: list[tuple[JobKind, str, str, str]],
    ) -> None:
        del actor_id
        turn = dag.require_turn(failure.turn_id)
        subtasks = dag.list_subtasks(failure.turn_id)
        if not subtasks:
            # Decomposition never succeeded: nothing to answer partially.
            dag.mark_turn_state(
                failure.turn_id,
                "failed",
                expected={"waiting_model_decision"},
            )
            return
        dag.cancel_active_subtasks(
            failure.turn_id, "user chose to terminate the turn"
        )
        dag.mark_turn_state(
            failure.turn_id,
            "partial",
            expected={
                "waiting_model_decision",
                "running",
                "scheduling",
                "waiting_tool_approval",
            },
        )
        command_id = turn_synthesize_command_id(failure.turn_id)
        if task_repository.get_job_run(command_id) is None:
            job_row = task_repository.add_job_run(
                turn.task_id or "", command_id
            )
            job_row.job_kind = JobKind.TURN_SYNTHESIZE.value
            job_row.turn_id = failure.turn_id
            session.flush()
            to_publish.append(
                (JobKind.TURN_SYNTHESIZE, failure.turn_id, command_id, turn.task_id or "")
            )

    # ------------------------------------------------------------------ replanning

    def _create_replan_turn(
        self,
        session: Session,
        dag: DagRepository,
        task_repository: TaskRepository,
        source_turn: ConversationTurnRow,
        actor_id: str,
        to_publish: list[tuple[JobKind, str, str, str]],
        goal: str | None = None,
    ) -> str:
        del actor_id  # the compatibility task and turn are owned by the owner
        from secagent.auth.dependencies import AuthenticatedUser
        from secagent.db_models import ConversationRow, UserRow

        conversation = session.get(ConversationRow, source_turn.conversation_id)
        if conversation is None:  # pragma: no cover - turn implies conversation
            raise KeyError(source_turn.conversation_id)
        owner_row = session.get(UserRow, conversation.owner_id)
        from secagent.domain import UserRole

        owner = AuthenticatedUser(
            id=conversation.owner_id,
            username=owner_row.username if owner_row else "owner",
            role=UserRole(owner_row.role) if owner_row else UserRole.ANALYST,
        )
        repository = ConversationRepository(session)
        task = task_repository.create_task(
            TaskCreate(
                goal=(goal or f"Replan of turn {source_turn.id}")[:4000],
                authorization_scope="Conversation replan",
            ),
            owner_id=conversation.owner_id,
            commit=False,
        )
        turn = repository.create_replan_turn(
            owner,
            conversation_id=source_turn.conversation_id,
            source_turn_id=source_turn.id,
            task_id=task.id,
            budget=TurnBudgetSnapshot.model_validate_json(source_turn.budget_json),
            commit=False,
        )
        command_id = turn_decompose_command_id(turn.id, turn.plan_version)
        job_row = task_repository.add_job_run(task.id, command_id)
        job_row.job_kind = JobKind.TURN_DECOMPOSE.value
        job_row.turn_id = turn.id
        session.flush()
        to_publish.append((JobKind.TURN_DECOMPOSE, turn.id, command_id, task.id))
        return turn.id

    def auto_continue_partial(self, source_turn_id: str, goal: str) -> str | None:
        """Create a continuation turn for a partial result, or None if capped.

        Used to keep analysing a turn until the synthesis is complete (no
        unresolved items), so an autonomous CTF/web flow follows promising
        leads instead of settling for a partial answer. Capped by
        ``max_auto_continues`` so it degrades to a partial result rather than
        looping across the whole budget.
        """
        to_publish: list[tuple[JobKind, str, str, str]] = []
        with self.session_factory() as session:
            dag = DagRepository(session)
            task_repository = TaskRepository(session)
            source_turn = dag.require_turn(source_turn_id)
            if self._replan_depth(session, source_turn_id) >= self.max_auto_continues:
                return None
            new_turn_id = self._create_replan_turn(
                session,
                dag,
                task_repository,
                source_turn,
                "system",
                to_publish,
                goal=goal,
            )
            session.commit()
        self._publish(to_publish)
        return new_turn_id

    @staticmethod
    def _replan_depth(session: Session, turn_id: str) -> int:
        depth = 0
        current = session.get(ConversationTurnRow, turn_id)
        while current is not None and current.replan_from_turn_id:
            depth += 1
            current = session.get(ConversationTurnRow, current.replan_from_turn_id)
        return depth

    def replan_after_message(
        self,
        *,
        conversation_id: str,
        source_turn_id: str,
        actor: Any,
        goal: str,
        authorization_scope: str,
        safety_mode: str,
        owner_id: str,
    ) -> str:
        """Follow-up message during an active turn: supersede and replan.

        Must be called inside the caller's open transaction; returns the new
        turn id and leaves the decompose job row to publish post-commit.
        """
        to_publish: list[tuple[JobKind, str, str, str]] = []
        with self.session_factory() as session:
            dag = DagRepository(session)
            task_repository = TaskRepository(session)
            source_turn = dag.require_turn(source_turn_id)
            dag.supersede_active_subtasks(
                source_turn_id, "superseded by a follow-up message"
            )
            dag.mark_turn_state(
                source_turn_id,
                "replan_requested",
                expected={
                    "running",
                    "scheduling",
                    "decomposing",
                    "waiting_tool_approval",
                    "waiting_model_decision",
                },
            )
            repository = ConversationRepository(session)
            task = task_repository.create_task(
                TaskCreate(
                    goal=goal,
                    authorization_scope=authorization_scope,
                    safety_mode=safety_mode,
                ),
                owner_id=owner_id,
                commit=False,
            )
            turn = repository.create_replan_turn(
                actor,
                conversation_id=conversation_id,
                source_turn_id=source_turn_id,
                task_id=task.id,
                budget=TurnBudgetSnapshot.model_validate_json(
                    source_turn.budget_json
                ),
                commit=False,
            )
            command_id = turn_decompose_command_id(turn.id, turn.plan_version)
            job_row = task_repository.add_job_run(task.id, command_id)
            job_row.job_kind = JobKind.TURN_DECOMPOSE.value
            job_row.turn_id = turn.id
            session.flush()
            new_turn_id = turn.id
            session.commit()
        self._publish(
            [(JobKind.TURN_DECOMPOSE, new_turn_id, command_id, task.id)]
        )
        return new_turn_id

    # ----------------------------------------------------------------- approvals

    def decide_subtask_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        reason: str,
        actor_id: str,
    ) -> ApprovalRow:
        to_publish: list[tuple[JobKind, str, str, str]] = []
        with self.session_factory() as session:
            dag = DagRepository(session)
            task_repository = TaskRepository(session)
            row = session.get(ApprovalRow, approval_id)
            if row is None or row.turn_id is None:
                raise UnknownApproval(approval_id)
            if row.status != "pending":
                raise ApprovalConflict(approval_id)
            decided = task_repository.decide_latest_approval(
                row.task_id,
                approved=approved,
                reason=reason,
                decided_by=actor_id,
                commit=False,
            )
            if decided.id != approval_id:
                raise ApprovalConflict(approval_id)
            subtask = dag.get_subtask(row.subtask_id or "")
            if subtask is None:
                raise UnknownApproval(approval_id)
            if approved:
                if subtask.status is SubtaskStatus.WAITING_TOOL_APPROVAL:
                    # The retry re-runs _run_loop which transitions the subtask
                    # to RUNNING from QUEUED, so it must be re-queued here;
                    # otherwise the resumed job fails on the illegal transition
                    # and the approved tool never actually executes.
                    dag.transition_subtask(
                        subtask.id,
                        SubtaskStatus.QUEUED,
                        expected={SubtaskStatus.WAITING_TOOL_APPROVAL},
                        reason="tool approved by operator; re-queued for execution",
                    )
                    self._enqueue_subtask_retry(
                        session, dag, task_repository, subtask, to_publish
                    )
                    dag.mark_turn_state(
                        subtask.turn_id,
                        "running",
                        expected={"waiting_tool_approval"},
                    )
            else:
                try:
                    dag.transition_subtask(
                        subtask.id,
                        SubtaskStatus.CANCELLED,
                        expected=_ACTIVE_SUBTASK_STATES,
                        reason="tool approval rejected",
                    )
                except SubtaskTransitionError:
                    pass
            session.commit()
            decided_row = decided
        self._publish(to_publish)
        return decided_row

    # ------------------------------------------------------------------ stopping

    def request_stop(self, conversation_id: str, actor: Any) -> str:
        """Stop the active turn at the next safe checkpoint."""
        with self.session_factory() as session:
            repository = ConversationRepository(session)
            conversation = repository.get_conversation(actor, conversation_id)
            if conversation is None:
                raise KeyError(conversation_id)
            dag = DagRepository(session)
            if conversation.active_turn_id is None:
                raise ModelFailureConflict("conversation has no active turn")
            turn = dag.require_turn(conversation.active_turn_id)
            if turn.status not in {
                "created",
                "decomposing",
                "scheduling",
                "running",
                "waiting_tool_approval",
                "waiting_model_decision",
            }:
                raise ModelFailureConflict(
                    f"turn {turn.id} is already {turn.status}"
                )
            dag.cancel_active_subtasks(
                turn.id, "user requested stop at checkpoint"
            )
            dag.mark_turn_state(
                turn.id,
                "cancelled",
                expected={
                    "created",
                    "decomposing",
                    "scheduling",
                    "running",
                    "waiting_tool_approval",
                    "waiting_model_decision",
                },
            )
            dag.record_turn_event(
                turn.id,
                "turn.cancelled",
                {"turn_id": turn.id, "reason": "user_requested_stop"},
            )
            session.commit()
            return "cancelled"

    # ------------------------------------------------------------------ internals

    def _enqueue_subtask_retry(
        self,
        session: Session,
        dag: DagRepository,
        task_repository: TaskRepository,
        subtask: Any,
        to_publish: list[tuple[JobKind, str, str, str]],
    ) -> str:
        turn = dag.require_turn(subtask.turn_id)
        next_attempt = self._next_attempt_number(session, subtask.id)
        command_id = subtask_execute_command_id(subtask.id, next_attempt)
        job_row = task_repository.add_job_run(turn.task_id or "", command_id)
        job_row.job_kind = JobKind.SUBTASK_EXECUTE.value
        job_row.turn_id = subtask.turn_id
        job_row.subtask_id = subtask.id
        session.flush()
        to_publish.append(
            (JobKind.SUBTASK_EXECUTE, subtask.id, command_id, turn.task_id or "")
        )
        return command_id

    @staticmethod
    def _original_failed_command(
        session: Session, turn_id: str
    ) -> str | None:
        row = session.scalar(
            select(JobRunRow)
            .where(
                JobRunRow.turn_id == turn_id,
                JobRunRow.status == "failed",
                JobRunRow.job_kind.in_(
                    [
                        JobKind.TURN_DECOMPOSE.value,
                        JobKind.TURN_SYNTHESIZE.value,
                    ]
                ),
            )
            .order_by(JobRunRow.created_at.desc())
            .limit(1)
        )
        return row.command_id if row is not None else None

    @staticmethod
    def _next_attempt_number(session: Session, subtask_id: str) -> int:
        from sqlalchemy import func

        value = session.scalar(
            select(func.max(JobRunRow.attempt)).where(
                JobRunRow.subtask_id == subtask_id,
                JobRunRow.job_kind == JobKind.SUBTASK_EXECUTE.value,
            )
        )
        return int(value or 0) + 1

    def _publish(
        self, to_publish: list[tuple[JobKind, str, str, str]]
    ) -> None:
        if not to_publish:
            return
        with self.session_factory() as session:
            task_repository = TaskRepository(session)
            for kind, ref_id, command_id, task_id in to_publish:
                state = task_repository.begin_job_publish(command_id)
                if state != "claimed":
                    continue
                try:
                    broker_id = self.queue.enqueue_dag_job(
                        kind, ref_id, command_id
                    )
                except Exception:
                    task_repository.mark_job_enqueue_failed(task_id, command_id)
                    task_repository.commit()
                    continue
                task_repository.mark_job_enqueued(command_id, broker_id)

