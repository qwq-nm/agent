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
from sqlalchemy import select
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
    ConversationSettings,
    TurnBudgetSnapshot,
)
from secagent.dag_domain import (
    JobKind,
    ModelFailureCreate,
    ModelFailureStage,
    SubtaskStatus,
)
from secagent.db import make_session_factory
from secagent.db_models import (
    ConversationMessageRow,
    ConversationRow,
    ConversationTurnRow,
    JobRunRow,
    MessageAttachmentRow,
    SubtaskRow,
)
from secagent.domain import ModelStage
from secagent.providers.base import ProviderUnavailable
from secagent.providers.runtime import ProviderRuntimeFactory
from secagent.repositories.dag_repository import DagRepository
from secagent.repository import TaskRepository
from secagent.security.redaction import redact_text
from secagent.services.assignment_policy import AssignmentPolicy
from secagent.services.job_service import JobLease, JobService
from secagent.services.ledger import LedgerService
from secagent.services.tool_authorization import derive_authorized_tools
from secagent.tools.registry import ToolRegistry

if TYPE_CHECKING:  # pragma: no cover
    from secagent.services.dag_scheduler import DagScheduler

_DAG_JOB_KINDS = {
    JobKind.TURN_DECOMPOSE.value,
    JobKind.SUBTASK_EXECUTE.value,
    JobKind.TURN_SYNTHESIZE.value,
}


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
        try:
            with self.session_factory() as session:
                router = self.router_builder(session)
            result = await self._decompose_turn(
                turn_id, plan_version, task_id, lease, router
            )
            with self.session_factory() as session:
                JobService(
                    TaskRepository(session), None, lease_seconds=self.lease_seconds
                ).finish(
                    lease.job_run_id,
                    lease.worker_id,
                    "completed" if result else "failed",
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
            dag.mark_turn_state(turn_id, "decomposing", expected={"created"})
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
            task_repository = TaskRepository(session)
            ledger = LedgerService(task_repository)
            ledger.record_model_response(
                task_id,
                ModelStage.DECOMPOSE,
                result.model_response,
                turn_id=turn_id,
            )
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
    scheduler: "DagScheduler | None" = None,
) -> None:
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
        for row in rows:
            task_repository = TaskRepository(session)
            if row.status == "enqueue_failed":
                task_repository.claim_job_republish(row.command_id)
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
