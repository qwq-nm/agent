"""Turn orchestration: the decompose job boundary.

The orchestrator persists what the pure CoordinatorAgent produces, transitions
the turn state machine, and records model failures without ever falling back
to another provider. Scheduling itself lives in DagScheduler.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from secagent.agents.coordinator import (
    CoordinatorAgent,
    CoordinatorAttachment,
    CoordinatorEvidence,
    CoordinatorRecentMessage,
    CoordinatorResult,
    CoordinatorTool,
    DecompositionContext,
)
from secagent.conversation_domain import (
    ConversationMessageKind,
    ConversationMessageRole,
    ConversationMessageStatus,
    ConversationMessageWrite,
    ConversationSettings,
    TurnBudgetSnapshot,
)
from secagent.conversation_repository import ConversationRepository
from secagent.dag_domain import (
    JobKind,
    ModelFailureCreate,
    ModelFailureStage,
    SubtaskStatus,
    SubtaskTransitionError,
)
from secagent.db import make_session_factory
from secagent.db_models import (
    ConversationMessageRow,
    ConversationRow,
    ConversationTurnRow,
    JobRunRow,
    MessageAttachmentRow,
    SubtaskRow,
    TaskRow,
    UserRow,
)
from secagent.domain import ModelStage, UserRole
from secagent.providers.base import ProviderUnavailable
from secagent.providers.runtime import ProviderRuntimeFactory
from secagent.repositories.dag_repository import DagRepository
from secagent.repository import TaskRepository
from secagent.security.redaction import redact_text
from secagent.services.assignment_policy import AssignmentPolicy
from secagent.services.chitchat import quick_chitchat_reply
from secagent.services.job_service import JobLease, JobService
from secagent.services.ledger import LedgerService
from secagent.services.runtime_memory import build_conversation_memory
from secagent.services.tool_authorization import derive_authorized_tools
from secagent.tools.registry import ToolRegistry

if TYPE_CHECKING:  # pragma: no cover
    from secagent.services.dag_scheduler import DagScheduler

_DAG_JOB_KINDS = {
    JobKind.TURN_DECOMPOSE.value,
    JobKind.SUBTASK_EXECUTE.value,
    JobKind.TURN_SYNTHESIZE.value,
}

#: Discovery tools injected into a web/CTF subtask whose decomposition forgot
#: to assign any tool. Guarantees the solver can at least fetch and explore the
#: authorized target instead of stalling on an empty ``allowed_tools``.
_WEB_DISCOVERY_TOOLS = (
    "url_guard",
    "http_fetch",
    "header_check",
    "form_extract",
    "link_extract",
    "browser_snapshot",
    "dirsearch_scan",
    "robots_analyzer",
    "js_analyzer",
    "path_normalizer",
    "flag_pattern_detector",
    "cookie_analyzer",
    "sensitive_file_checker",
    "login_probe",
    "sqlmap_probe",
    "submit_flag",
)


def build_decomposition_context(
    session: Session,
    *,
    registry: ToolRegistry,
    turn_id: str,
) -> DecompositionContext:
    dag = DagRepository(session)
    turn = dag.require_turn(turn_id)
    conversation = session.get(ConversationRow, turn.conversation_id)
    if conversation is None:  # pragma: no cover - turn implies conversation
        raise KeyError(turn.conversation_id)
    trigger = session.get(ConversationMessageRow, turn.trigger_message_id)
    if trigger is None:  # pragma: no cover - turn implies trigger message
        raise KeyError(turn.trigger_message_id)

    recent_rows = session.scalars(
        select(ConversationMessageRow)
        .where(
            ConversationMessageRow.conversation_id == conversation.id,
            ConversationMessageRow.sequence < trigger.sequence,
            ConversationMessageRow.role != ConversationMessageRole.SYSTEM.value,
        )
        .order_by(ConversationMessageRow.sequence.desc())
        .limit(40)
    ).all()
    recent = [
        CoordinatorRecentMessage(
            role=(
                "assistant"
                if row.role == ConversationMessageRole.ASSISTANT.value
                else "user"
            ),
            content=row.content,
        )
        for row in reversed(recent_rows)
        if row.kind
        in (
            ConversationMessageKind.USER_TEXT.value,
            ConversationMessageKind.ASSISTANT_ANSWER.value,
        )
    ]

    attachment_rows = session.scalars(
        select(MessageAttachmentRow).where(
            MessageAttachmentRow.message_id == trigger.id
        )
    ).all()
    attachments = [
        CoordinatorAttachment(
            original_name=row.original_name,
            relative_path=row.relative_path,
            content_type=row.content_type,
            sha256=row.sha256,
            scan_summary=(
                f"attachment stored read-only; {row.size_bytes} bytes; "
                f"sha256 verified; status {row.status}"
            ),
        )
        for row in attachment_rows
    ]

    settings = ConversationSettings.model_validate_json(conversation.settings_json)
    available_tools = [
        CoordinatorTool(
            name=spec["name"],
            risk_level=spec["risk_level"],
            description=redact_text(str(spec.get("description", "安全分析工具"))),
        )
        for spec in registry.describe()
    ][:128]

    return DecompositionContext(
        current_message=trigger.content[:64_000],
        conversation_summary="",
        recent_messages=recent,
        attachments=attachments,
        settings=settings,
        completed_subtasks=_completed_subtasks(session, turn),
        evidence=_completed_evidence(session, turn),
        unresolved_questions=[],
        available_tools=available_tools,
        runtime_memory=build_conversation_memory(
            session, conversation_id=conversation.id
        ),
        budget=TurnBudgetSnapshot.model_validate_json(turn.budget_json),
    )


def _completed_subtasks(session: Session, turn: ConversationTurnRow) -> list[Any]:
    if turn.replan_from_turn_id is None:
        return []
    rows = session.scalars(
        select(SubtaskRow).where(
            SubtaskRow.turn_id == turn.replan_from_turn_id,
            SubtaskRow.status == SubtaskStatus.COMPLETED.value,
        )
    ).all()
    from secagent.agents.coordinator import CoordinatorCompletedSubtask

    return [
        CoordinatorCompletedSubtask(
            key=row.key,
            provider=row.assigned_provider,
            summary=row.result_summary or row.expected_output,
            evidence_refs=[],
        )
        for row in rows
    ]


def _completed_evidence(session: Session, turn: ConversationTurnRow) -> list[Any]:
    if turn.replan_from_turn_id is None:
        return []
    from secagent.db_models import EvidenceRow

    rows = session.scalars(
        select(EvidenceRow)
        .where(EvidenceRow.turn_id == turn.replan_from_turn_id)
        .order_by(EvidenceRow.created_at.asc())
        .limit(128)
    ).all()
    return [
        CoordinatorEvidence(
            ref=row.source[:128],
            summary=row.content[:4_000] or "stored evidence",
            confidence=max(0.0, min(1.0, float(row.confidence))),
        )
        for row in rows
    ]


class TurnOrchestratorService:
    """Runs the durable decompose job for one conversation turn."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        router_builder: Callable[[Session], Any],
        registry: ToolRegistry,
        settings: Any,
        scheduler: "DagScheduler | None" = None,
        lease_seconds: int = 90,
        heartbeat_seconds: int = 15,
    ) -> None:
        self.session_factory = session_factory
        self.router_builder = router_builder
        self.registry = registry
        self.settings = settings
        self.scheduler = scheduler
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds

    async def run_decompose_job(
        self, turn_id: str, command_id: str, worker_id: str
    ) -> None:
        with self.session_factory() as session:
            turn = DagRepository(session).require_turn(turn_id)
            task_id = turn.task_id
            plan_version = turn.plan_version
        if task_id is None:
            raise ValueError(f"turn {turn_id} has no compatible task row")

        claim_session = self.session_factory()
        try:
            lease = JobService(
                TaskRepository(claim_session), None, lease_seconds=self.lease_seconds
            ).claim(task_id, command_id, worker_id)
        except Exception:
            claim_session.close()
            raise
        if lease is None:
            claim_session.close()
            return
        try:
            await self._execute(
                turn_id, plan_version, task_id, command_id, lease
            )
        finally:
            claim_session.close()

    async def _execute(
        self,
        turn_id: str,
        plan_version: int,
        task_id: str,
        command_id: str,
        lease: JobLease,
    ) -> None:
        heartbeat = asyncio.create_task(
            self._heartbeat(lease, self.session_factory)
        )
        router = None
        job_status = "failed"
        try:
            with self.session_factory() as session:
                router = self.router_builder(session)
            try:
                result = await self._decompose_turn(
                    turn_id, plan_version, task_id, lease, router
                )
                job_status = "completed" if result else "failed"
            finally:
                # The job must always land in a final state, even when the
                # decompose body raises (e.g. the turn was superseded mid-call);
                # otherwise recover_expired keeps re-queueing a zombie job.
                with suppress(Exception):
                    with self.session_factory() as session:
                        JobService(
                            TaskRepository(session),
                            None,
                            lease_seconds=self.lease_seconds,
                        ).finish(
                            lease.job_run_id,
                            lease.worker_id,
                            job_status,
                        )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            if router is not None:
                with suppress(Exception):
                    await router.aclose()

    async def _heartbeat(
        self, lease: JobLease, session_factory: sessionmaker[Session]
    ) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            with session_factory() as session:
                renewed = JobService(
                    TaskRepository(session), None, lease_seconds=self.lease_seconds
                ).heartbeat(lease.job_run_id, lease.worker_id)
            if renewed is None:
                return

    async def _decompose_turn(
        self,
        turn_id: str,
        plan_version: int,
        task_id: str,
        lease: JobLease,
        router: Any,
    ) -> bool:
        del lease  # fence checks happen at claim/finish boundaries
        with self.session_factory() as session:
            dag = DagRepository(session)
            try:
                dag.mark_turn_state(
                    turn_id,
                    "decomposing",
                    expected={"created", "waiting_model_decision"},
                )
            except SubtaskTransitionError:
                # A follow-up message superseded this turn before the worker
                # reached the decompose boundary. Do not let the exception
                # escape: discard the turn, finish the job, and let the newer
                # plan version own the conversation.
                self._discard_turn(
                    turn_id,
                    "turn was superseded before decomposition started",
                )
                return True
            dag.record_turn_event(
                turn_id,
                "turn.decomposition.started",
                {"turn_id": turn_id, "plan_version": plan_version},
            )
            session.commit()

        with self.session_factory() as session:
            context = build_decomposition_context(
                session, registry=self.registry, turn_id=turn_id
            )

        # Fast path: a plain greeting completes the turn immediately with a
        # static reply instead of spending 100+ seconds on DeepSeek
        # decomposition plus real subtasks.
        chitchat_reply = quick_chitchat_reply(context)
        if chitchat_reply is not None:
            return self._complete_chitchat_turn(turn_id, chitchat_reply)

        registered = frozenset(spec["name"] for spec in self.registry.describe())
        try:
            authorized = derive_authorized_tools(
                context.settings.safety_mode, self.registry
            )
            coordinator = CoordinatorAgent(router, AssignmentPolicy())
            result: CoordinatorResult = await coordinator.decompose(
                context,
                plan_version=plan_version,
                registered_tools=registered,
                authorized_tools=authorized,
            )
        except (ProviderUnavailable, ValidationError, ValueError) as exc:
            self._record_decompose_failure(turn_id, task_id, exc)
            return False

        with self.session_factory() as session:
            dag = DagRepository(session)
            current = dag.require_turn(turn_id)
            if current.status != "decomposing":
                # The turn was superseded while the model call was in flight.
                # Discard this stale plan (do not create subtasks, do not
                # schedule) and let the newer plan version take over.
                self._discard_turn(
                    turn_id,
                    f"turn was superseded during decomposition ({current.status})",
                )
                return True
            task_repository = TaskRepository(session)
            ledger = LedgerService(task_repository)
            ledger.record_model_response(
                task_id,
                ModelStage.DECOMPOSE,
                result.model_response,
                turn_id=turn_id,
            )
            # Direction A: a web/CTF task is solved by ONE end-to-end solver
            # subtask, not a graph of narrow subtasks. Collapse any multi-subtask
            # decomposition to just the first subtask, then union the full
            # discovery toolset onto it so the solver can act freely.
            task_row = session.get(TaskRow, task_id) if task_id else None
            if task_row is not None and task_row.target_url:
                if len(result.document.subtasks) > 1:
                    keep = result.document.subtasks[0]
                    result.document.subtasks = [keep]
                    result.assignments = [
                        item for item in result.assignments if item.key == keep.key
                    ]
                discovery = [name for name in _WEB_DISCOVERY_TOOLS if name in registered]
                for decision in result.assignments:
                    merged = list(decision.allowed_tools)
                    for name in discovery:
                        if name not in merged:
                            merged.append(name)
                    decision.allowed_tools = merged
            subtasks = dag.create_subtasks_from_document(
                turn_id, result.document, result.assignments
            )
            dag.record_turn_event(
                turn_id,
                "turn.plan.versioned",
                {
                    "turn_id": turn_id,
                    "plan_version": plan_version,
                    "subtask_keys": [item.key for item in subtasks],
                },
            )
            dag.mark_turn_state(turn_id, "scheduling", expected={"decomposing"})
            session.commit()

        if self.scheduler is not None:
            self.scheduler.schedule_turn(turn_id)
        return True

    def _discard_turn(self, turn_id: str, reason: str) -> None:
        """Record that a stale turn was discarded and leave it untouched.

        The turn keeps whatever state the superseding flow set (normally
        ``replan_requested`` or ``superseded``); only the event is appended.
        """
        with self.session_factory() as session:
            dag = DagRepository(session)
            dag.record_turn_event(
                turn_id,
                "turn.decomposition.discarded",
                {"turn_id": turn_id, "reason": reason},
            )
            session.commit()

    def _complete_chitchat_turn(self, turn_id: str, reply: str) -> bool:
        """Complete a chitchat turn: guard the state, then write the reply.

        The state guard is separate from the reply write so a turn that was
        superseded between context build and reply write is discarded instead
        of crashing on an illegal transition.
        """
        with self.session_factory() as session:
            dag = DagRepository(session)
            try:
                dag.mark_turn_state(
                    turn_id,
                    "completed",
                    expected={"decomposing"},
                )
                session.commit()
            except SubtaskTransitionError:
                session.rollback()
                self._discard_turn(
                    turn_id,
                    "turn was superseded before the chitchat reply was written",
                )
                return True
        self._write_chitchat_reply(turn_id, reply)
        return True

    def _write_chitchat_reply(self, turn_id: str, reply: str) -> None:
        """Persist the assistant answer message and completion events."""
        from secagent.auth.dependencies import AuthenticatedUser

        with self.session_factory() as session:
            dag = DagRepository(session)
            turn = dag.require_turn(turn_id)
            conversation_id = turn.conversation_id
            conversation = session.get(ConversationRow, conversation_id)
            if conversation is None:  # pragma: no cover - turn implies conversation
                raise KeyError(conversation_id)
            owner = session.get(UserRow, conversation.owner_id)
            if owner is None:  # pragma: no cover - conversation implies owner
                raise KeyError(conversation.owner_id)
            actor = AuthenticatedUser(
                id=owner.id,
                username=owner.username,
                role=UserRole(owner.role),
            )
            repository = ConversationRepository(session)
            message = repository.add_message(
                actor,
                conversation_id,
                ConversationMessageWrite(
                    role=ConversationMessageRole.ASSISTANT,
                    kind=ConversationMessageKind.ASSISTANT_ANSWER,
                    content=reply,
                    status=ConversationMessageStatus.COMPLETED,
                    turn_id=turn_id,
                ),
                commit=False,
            )
            dag.record_turn_event(
                turn_id,
                "assistant.answer.completed",
                {
                    "turn_id": turn_id,
                    "message_id": message.message.id,
                    "evidence_refs": [],
                    "is_partial": False,
                },
            )
            dag.record_turn_event(
                turn_id,
                "turn.completed",
                {"turn_id": turn_id, "is_demo": False},
            )
            session.commit()

    def _record_decompose_failure(
        self,
        turn_id: str,
        task_id: str,
        exc: Exception,
    ) -> None:
        provider, model, error_code = "deepseek", "deepseek-v4-flash", "server"
        if isinstance(exc, ProviderUnavailable):
            provider = getattr(exc, "provider", provider) or provider
            code = getattr(exc, "code", None)
            error_code = getattr(code, "value", "server") or "server"
        elif isinstance(exc, ValidationError):
            error_code = "invalid_schema"
        with self.session_factory() as session:
            dag = DagRepository(session)
            task_repository = TaskRepository(session)
            ledger = LedgerService(task_repository)
            ledger.record_model_error(
                task_id,
                ModelStage.DECOMPOSE,
                provider=provider,
                model=model,
                error_code=error_code,
                request_id=getattr(exc, "request_id", None),
                turn_id=turn_id,
            )
            dag.record_model_failure(
                ModelFailureCreate(
                    turn_id=turn_id,
                    subtask_id=None,
                    stage=ModelFailureStage.DECOMPOSE,
                    provider=provider,
                    model=model,
                    error_code=error_code,
                    detail=redact_text(
                        f"{type(exc).__name__}: {exc}"[:1_800]
                    ),
                )
            )
            with suppress(SubtaskTransitionError):
                dag.mark_turn_state(
                    turn_id,
                    "waiting_model_decision",
                    expected={
                        "created",
                        "decomposing",
                        "scheduling",
                        "running",
                    },
                )
            session.commit()


async def execute_turn_decompose_job(
    turn_id: str,
    command_id: str,
    session_factory: sessionmaker[Session],
    registry: ToolRegistry,
    settings: Any,
    *,
    worker_id: str | None = None,
    queue: Any | None = None,
) -> None:
    from secagent.services.dag_scheduler import DagScheduler

    scheduler: DagScheduler | None = None
    if queue is not None:
        scheduler = DagScheduler(
            session_factory=session_factory,
            queue=queue,
            max_parallel=settings.max_parallel_subtasks_per_conversation,
        )

    def router_builder(session: Session) -> Any:
        return ProviderRuntimeFactory(settings).build(session)

    service = TurnOrchestratorService(
        session_factory=session_factory,
        router_builder=router_builder,
        registry=registry,
        settings=settings,
        scheduler=scheduler,
        lease_seconds=settings.job_lease_seconds,
        heartbeat_seconds=settings.job_heartbeat_seconds,
    )
    await service.run_decompose_job(
        turn_id, command_id, worker_id or "decompose-worker"
    )


def republish_pending_dag_jobs(
    session_factory: sessionmaker[Session],
    queue: Any,
) -> int:
    """Re-publish DAG jobs that never reached the broker (startup recovery)."""
    published = 0
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(JobRunRow)
                .where(
                    JobRunRow.job_kind.in_(_DAG_JOB_KINDS),
                    JobRunRow.status.in_(("pending_publish", "publishing", "enqueue_failed")),
                )
                .order_by(JobRunRow.created_at.asc())
            ).all()
        )
        superseded_turns = {
            "replan_requested",
            "superseded",
            "cancelled",
            "failed",
            "failed_retryable",
            "completed",
            "partial",
        }
        for row in rows:
            task_repository = TaskRepository(session)
            # A stale DAG job whose turn was superseded must not be
            # republished: the worker would crash on the illegal transition
            # and restart the zombie loop. Mark it failed instead.
            if row.turn_id is not None:
                turn = session.get(ConversationTurnRow, row.turn_id)
                if turn is not None and turn.status in superseded_turns:
                    from datetime import datetime, timezone

                    session.execute(
                        update(JobRunRow)
                        .where(JobRunRow.id == row.id)
                        .values(
                            status="failed",
                            worker_id=None,
                            heartbeat_at=None,
                            lease_expires_at=None,
                            finished_at=datetime.now(timezone.utc),
                        )
                    )
                    session.commit()
                    continue
            if row.status == "enqueue_failed":
                task_repository.claim_job_republish(row.command_id)
                # A re-queued DAG job means its compat task is executable
                # again; the legacy claim path refuses anything but QUEUED.
                from contextlib import suppress
                from secagent.domain import TaskStatus

                for previous in (TaskStatus.FAILED_RETRYABLE, TaskStatus.CREATED):
                    with suppress(ValueError):
                        task_repository.transition_task_status(
                            row.task_id,
                            previous,
                            TaskStatus.QUEUED,
                            commit=False,
                        )
                        break
            state = task_repository.begin_job_publish(row.command_id)
            if state != "claimed":
                continue
            kind = JobKind(row.job_kind)
            ref_id = (
                row.turn_id
                if kind is JobKind.TURN_DECOMPOSE
                or kind is JobKind.TURN_SYNTHESIZE
                else row.subtask_id
            )
            if ref_id is None:  # pragma: no cover - rows always carry a ref
                continue
            try:
                broker_id = queue.enqueue_dag_job(kind, ref_id, row.command_id)
            except Exception:
                task_repository.mark_job_enqueue_failed(row.task_id, row.command_id)
                continue
            task_repository.mark_job_enqueued(row.command_id, broker_id)
            published += 1
    return published

# Re-exported for the worker entry points; kept here to preserve the import
# surface used by secagent.worker.
from secagent.services.synthesis_service import (  # noqa: E402,F401
    execute_turn_synthesize_job,
)
