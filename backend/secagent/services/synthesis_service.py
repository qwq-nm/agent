"""DeepSeek-only final synthesis with evidence annotation and SSE streaming."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from secagent.auth.dependencies import AuthenticatedUser
from secagent.conversation_decomposition import RouteReasonCode  # noqa: F401
from secagent.conversation_domain import (
    ConversationMessageKind,
    ConversationMessageRole,
    ConversationMessageStatus,
    ConversationMessageWrite,
    canonical_json_dumps,
)
from secagent.conversation_repository import ConversationRepository
from secagent.dag_domain import (
    ModelFailureCreate,
    ModelFailureStage,
    SynthesisDocument,
)
from secagent.db import make_session_factory
from secagent.db_models import (
    ConversationMessageRow,
    ConversationRow,
    ConversationTurnRow,
    EvidenceRow,
    SubtaskResultRow,
    SubtaskRow,
    TaskRow,
    UserRow,
)
from secagent.domain import ModelStage
from secagent.providers.base import ProviderUnavailable
from secagent.providers.runtime import ProviderRuntimeFactory
from secagent.repositories.dag_repository import DagRepository
from secagent.repository import TaskRepository
from secagent.security.redaction import redact_mapping, redact_text
from secagent.services.ledger import LedgerService
from secagent.services.job_service import JobLease
from secagent.subtask_prompts import _WORKER_RESPONSE_SCHEMA  # noqa: F401

_SYNTHESIS_SYSTEM_PROMPT = (
    "You are the final synthesis stage. Combine the completed subtask results "
    "into one Chinese answer. Every factual statement must cite an evidence "
    "reference; anything unverified goes to inference_notes; missing inputs go "
    "to unresolved. Never invent facts and never call tools."
)

_SYNTHESIS_RESPONSE_SCHEMA = SynthesisDocument.model_json_schema()
_DELTA_CHUNK_SIZE = 160


def build_synthesis_request(
    *,
    goal_summary: str,
    synthesis_requirements: list[str],
    subtask_results: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "turn_goal": goal_summary,
        "synthesis_requirements": synthesis_requirements,
        "subtask_results": subtask_results,
        "evidence": evidence,
        "response_schema": _SYNTHESIS_RESPONSE_SCHEMA,
    }


async def execute_turn_synthesize_job(
    turn_id: str,
    command_id: str,
    session_factory: sessionmaker[Session],
    registry: Any,
    settings: Any,
    *,
    worker_id: str | None = None,
) -> None:
    service = SynthesisService(
        session_factory=session_factory,
        settings=settings,
        lease_seconds=settings.job_lease_seconds,
        heartbeat_seconds=settings.job_heartbeat_seconds,
    )
    await service.run(turn_id, command_id, worker_id or "synthesize-worker")


class SynthesisService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        settings: Any,
        lease_seconds: int = 90,
        heartbeat_seconds: int = 15,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds

    async def run(self, turn_id: str, command_id: str, worker_id: str) -> None:
        with self.session_factory() as session:
            turn = DagRepository(session).require_turn(turn_id)
            task_id = turn.task_id
        if task_id is None:
            raise ValueError(f"turn {turn_id} has no compatible task row")

        claim_session = self.session_factory()
        try:
            lease = DagRepository(claim_session).claim_dag_job(
                command_id, worker_id, lease_seconds=self.lease_seconds
            )
        except Exception:
            claim_session.close()
            raise
        if lease is None:
            claim_session.close()
            return
        try:
            await self._execute(turn_id, task_id, command_id, lease)
        finally:
            claim_session.close()

    # ------------------------------------------------------------------ internals

    async def _execute(
        self, turn_id: str, task_id: str, command_id: str, lease: JobLease
    ) -> None:
        heartbeat = asyncio.create_task(
            self._heartbeat(lease, self.session_factory)
        )
        router = None
        try:
            with self.session_factory() as session:
                router = ProviderRuntimeFactory(self.settings).build(session)
            try:
                await self._synthesize(turn_id, task_id, lease, router)
                job_status = "completed"
            except ProviderUnavailable as exc:
                self._record_failure(turn_id, task_id, exc)
                job_status = "failed"
            with self.session_factory() as session:
                DagRepository(session).finish_dag_job(
                    lease.job_run_id, lease.worker_id, job_status
                )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            if router is not None:
                with suppress(Exception):
                    await router.aclose()

    async def _synthesize(
        self, turn_id: str, task_id: str, lease: JobLease, router: Any
    ) -> None:
        with self.session_factory() as session:
            dag = DagRepository(session)
            turn = dag.require_turn(turn_id)
            conversation_id = turn.conversation_id
            dag.mark_turn_state(
                turn_id,
                "synthesizing",
                expected={
                    "running",
                    "scheduling",
                    "partial",
                    "waiting_tool_approval",
                    "synthesizing",  # idempotent rerun after a crashed attempt
                },
            )
            dag.record_turn_event(
                turn_id,
                "turn.synthesis.started",
                {"turn_id": turn_id, "plan_version": turn.plan_version},
            )
            subtask_results = self._collect_subtask_results(session, turn_id)
            evidence = self._collect_evidence(session, turn_id)
            goal_summary = self._goal_summary(session, turn_id)
            session.commit()

        payload = build_synthesis_request(
            goal_summary=goal_summary,
            synthesis_requirements=[],
            subtask_results=subtask_results,
            evidence=evidence,
        )
        from secagent.domain import ModelRequest

        request = ModelRequest(
            system=_SYNTHESIS_SYSTEM_PROMPT,
            user=canonical_json_dumps(payload),
            response_schema=_SYNTHESIS_RESPONSE_SCHEMA,
        )
        response = await router.complete(ModelStage.SYNTHESIZE, request)
        with self.session_factory() as session:
            LedgerService(TaskRepository(session)).record_model_response(
                task_id, ModelStage.SYNTHESIZE, response, turn_id=turn_id
            )
            session.commit()

        safe_data = redact_mapping(response.data)
        document = SynthesisDocument.model_validate(safe_data)
        self._stream_answer(turn_id, conversation_id, document, response.is_demo)

    def _stream_answer(
        self,
        turn_id: str,
        conversation_id: str,
        document: SynthesisDocument,
        is_demo: bool,
    ) -> None:
        with self.session_factory() as session:
            dag = DagRepository(session)
            owner_row = session.get(UserRow, self._owner_id(session, turn_id))
            from secagent.domain import UserRole

            actor = AuthenticatedUser(
                id=owner_row.id,
                username=owner_row.username,
                role=UserRole(owner_row.role),
            )
            repository = ConversationRepository(session)
            full_text = document.summary
            if document.is_partial or document.unresolved:
                full_text += "\n\n未完成范围：" + "；".join(document.unresolved)
            from secagent.db_models import ConversationMessageRow

            existing = session.scalar(
                select(ConversationMessageRow).where(
                    ConversationMessageRow.turn_id == turn_id,
                    ConversationMessageRow.kind
                    == ConversationMessageKind.ASSISTANT_ANSWER.value,
                )
            )
            if existing is not None:
                # Idempotent rerun after a crashed attempt: reuse the row.
                message_id = existing.id
            else:
                message = repository.add_message(
                    actor,
                    conversation_id,
                    ConversationMessageWrite(
                        role=ConversationMessageRole.ASSISTANT,
                        kind=ConversationMessageKind.ASSISTANT_ANSWER,
                        content=full_text,
                        status=ConversationMessageStatus.STREAMING,
                        turn_id=turn_id,
                    ),
                    commit=False,
                )
                message_id = message.message.id
                chunks = [
                    full_text[i : i + _DELTA_CHUNK_SIZE]
                    for i in range(0, len(full_text), _DELTA_CHUNK_SIZE)
                ] or [full_text]
                for seq, chunk in enumerate(chunks, start=1):
                    dag.record_turn_event(
                        turn_id,
                        "assistant.answer.delta",
                        {
                            "turn_id": turn_id,
                            "message_id": message_id,
                            "delta_seq": seq,
                            "text": chunk,
                        },
                    )
            dag.record_turn_event(
                turn_id,
                "assistant.answer.completed",
                {
                    "turn_id": turn_id,
                    "message_id": message_id,
                    "evidence_refs": sorted(
                        {
                            claim.evidence_ref
                            for claim in document.facts
                            if claim.evidence_ref
                        }
                    ),
                    "is_partial": document.is_partial,
                },
            )
            session.commit()

        with self.session_factory() as session:
            dag = DagRepository(session)
            dag.mark_turn_state(
                turn_id,
                "partial" if (document.is_partial or document.unresolved) else "completed",
                expected={"synthesizing"},
            )
            dag.record_turn_event(
                turn_id,
                "turn.completed",
                {"turn_id": turn_id, "is_demo": is_demo},
            )
            session.commit()

        with self.session_factory() as session:
            row = session.get(ConversationMessageRow, message_id)
            row.content = full_text
            row.status = "completed"
            session.commit()

    def _record_failure(
        self, turn_id: str, task_id: str, exc: ProviderUnavailable
    ) -> None:
        code = getattr(getattr(exc, "code", None), "value", "server") or "server"
        with self.session_factory() as session:
            dag = DagRepository(session)
            ledger = LedgerService(TaskRepository(session))
            provider = getattr(exc, "provider", "deepseek") or "deepseek"
            ledger.record_model_error(
                task_id,
                ModelStage.SYNTHESIZE,
                provider=provider,
                model="deepseek-v4-flash",
                error_code=code,
                request_id=getattr(exc, "request_id", None),
                turn_id=turn_id,
            )
            dag.record_model_failure(
                ModelFailureCreate(
                    turn_id=turn_id,
                    subtask_id=None,
                    stage=ModelFailureStage.SYNTHESIZE,
                    provider=provider,
                    model="deepseek-v4-flash",
                    error_code=code,
                    detail=redact_text(f"{type(exc).__name__}: {exc}"[:1_800]),
                )
            )
            with suppress(Exception):
                dag.mark_turn_state(
                    turn_id,
                    "waiting_model_decision",
                    expected={"synthesizing", "running", "partial"},
                )
            session.commit()

    # ----------------------------------------------------------------- collectors

    @staticmethod
    def _owner_id(session: Session, turn_id: str) -> str:
        conversation = (
            session.query(ConversationRow)
            .join(
                ConversationTurnRow,
                ConversationTurnRow.conversation_id == ConversationRow.id,
            )
            .filter(ConversationTurnRow.id == turn_id)
            .one()
        )
        return conversation.owner_id

    @staticmethod
    def _collect_subtask_results(session: Session, turn_id: str) -> list[dict[str, Any]]:
        rows = session.execute(
            select(SubtaskRow, SubtaskResultRow)
            .join(SubtaskResultRow, SubtaskResultRow.subtask_id == SubtaskRow.id)
            .where(SubtaskRow.turn_id == turn_id)
        ).all()
        return [
            {
                "key": subtask.key,
                "status": result.status,
                "summary": result.summary,
                "evidence_refs": json.loads(result.evidence_refs_json),
                "unresolved": json.loads(result.unresolved_json),
            }
            for subtask, result in rows
        ]

    @staticmethod
    def _collect_evidence(session: Session, turn_id: str) -> list[dict[str, Any]]:
        rows = session.scalars(
            select(EvidenceRow)
            .where(EvidenceRow.turn_id == turn_id)
            .order_by(EvidenceRow.created_at.asc())
            .limit(128)
        ).all()
        return [
            {
                "ref": row.source,
                "summary": row.content[:500],
                "confidence": row.confidence,
            }
            for row in rows
        ]

    @staticmethod
    def _goal_summary(session: Session, turn_id: str) -> str:
        turn = session.get(ConversationTurnRow, turn_id)
        if turn is None or turn.task_id is None:
            return ""
        task = session.get(TaskRow, turn.task_id)
        return task.goal if task is not None else ""

    async def _heartbeat(
        self, lease: JobLease, session_factory: sessionmaker[Session]
    ) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            with session_factory() as session:
                from secagent.services.job_service import JobService

                renewed = JobService(
                    TaskRepository(session),
                    None,
                    lease_seconds=self.lease_seconds,
                ).heartbeat(lease.job_run_id, lease.worker_id)
            if renewed is None:
                return
