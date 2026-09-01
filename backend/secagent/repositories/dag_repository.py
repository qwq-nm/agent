"""Conditional state transitions for the durable subtask DAG.

Every write is an expected-state-guarded UPDATE paired with a redacted
conversation event so the frontend can only ever observe real backend facts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from secagent.conversation_domain import canonical_json_dumps
from secagent.dag_domain import (
    ClaimDocument,
    ModelFailureAlreadyResolved,
    ModelFailureCreate,
    ModelFailureDecision,
    ModelFailureRead,
    ModelFailureStage,
    ModelFailureStatus,
    SubtaskAttemptRead,
    SubtaskRead,
    SubtaskResultDocument,
    SubtaskResultRead,
    SubtaskStatus,
    SubtaskTransitionError,
)
from secagent.dag_domain import JobKind, TERMINAL_SUBTASK_STATUSES
from secagent.db_models import (
    ConversationEventRow,
    ConversationTurnRow,
    JobRunRow,
    ModelFailureRow,
    SubtaskAttemptRow,
    SubtaskDependencyRow,
    SubtaskResultRow,
    SubtaskRow,
)
from secagent.services.assignment_policy import AssignmentDecision
from secagent.services.conversation_events import encode_redacted_event_payload

_EVENT_NAME_BY_STATUS: dict[SubtaskStatus, str] = {
    SubtaskStatus.PENDING_DEPENDENCY: "subtask.pending_dependency",
    SubtaskStatus.QUEUED: "subtask.queued",
    SubtaskStatus.RUNNING: "subtask.started",
    SubtaskStatus.WAITING_TOOL_APPROVAL: "subtask.waiting_approval",
    SubtaskStatus.WAITING_MODEL_DECISION: "subtask.waiting_model_decision",
    SubtaskStatus.COMPLETED: "subtask.completed",
    SubtaskStatus.INCOMPLETE: "subtask.incomplete",
    SubtaskStatus.SKIPPED: "subtask.skipped",
    SubtaskStatus.SUPERSEDED: "subtask.superseded",
    SubtaskStatus.FAILED: "subtask.failed",
    SubtaskStatus.CANCELLED: "subtask.cancelled",
}

# An upstream that reached a usable terminal result unlocks its dependents.
# "incomplete" still carries a summary and unresolved notes, so downstream
# workers consume it as weaker context; only a hard failure blocks the DAG.
_SUCCESS_STATUSES = (
    SubtaskStatus.COMPLETED.value,
    SubtaskStatus.INCOMPLETE.value,
)


class DagRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    # ------------------------------------------------------------------ reads

    def require_turn(self, turn_id: str) -> ConversationTurnRow:
        row = self.session.get(ConversationTurnRow, turn_id)
        if row is None:
            raise KeyError(turn_id)
        return row

    def get_subtask(self, subtask_id: str) -> SubtaskRead | None:
        row = self.session.get(SubtaskRow, subtask_id)
        return self._subtask_read(row) if row else None

    def require_subtask(self, subtask_id: str) -> SubtaskRow:
        row = self.session.get(SubtaskRow, subtask_id)
        if row is None:
            raise KeyError(subtask_id)
        return row

    def list_subtasks(self, turn_id: str) -> list[SubtaskRead]:
        rows = self.session.scalars(
            select(SubtaskRow)
            .where(SubtaskRow.turn_id == turn_id)
            .order_by(SubtaskRow.created_at.asc(), SubtaskRow.key.asc())
        ).all()
        return [self._subtask_read(row) for row in rows]

    def ready_subtasks(self, turn_id: str) -> list[SubtaskRead]:
        """Pending subtasks whose dependencies are all completed."""
        rows = self.session.scalars(
            select(SubtaskRow).where(
                SubtaskRow.turn_id == turn_id,
                SubtaskRow.status == SubtaskStatus.PENDING_DEPENDENCY.value,
            )
        ).all()
        if not rows:
            return []
        ids = [row.id for row in rows]
        dependency_rows = self.session.execute(
            select(
                SubtaskDependencyRow.subtask_id,
                SubtaskDependencyRow.dependency_subtask_id,
            ).where(SubtaskDependencyRow.subtask_id.in_(ids))
        ).all()
        blocking: dict[str, set[str]] = {}
        for subtask_id, dependency_id in dependency_rows:
            blocking.setdefault(subtask_id, set()).add(dependency_id)
        satisfied: dict[str, str] = {
            row.id: row.status
            for row in self.session.scalars(
                select(SubtaskRow).where(SubtaskRow.turn_id == turn_id)
            ).all()
        }
        ready: list[SubtaskRead] = []
        for row in rows:
            deps = blocking.get(row.id, set())
            if all(satisfied.get(dep) in _SUCCESS_STATUSES for dep in deps):
                ready.append(self._subtask_read(row))
        return ready

    def running_subtask_count(self, conversation_id: str) -> int:
        """In-flight subtasks (queued for the broker or running) per conversation."""
        return len(
            self.session.scalars(
                select(SubtaskRow.id)
                .join(
                    ConversationTurnRow,
                    SubtaskRow.turn_id == ConversationTurnRow.id,
                )
                .where(
                    ConversationTurnRow.conversation_id == conversation_id,
                    SubtaskRow.status.in_(
                        (
                            SubtaskStatus.QUEUED.value,
                            SubtaskStatus.RUNNING.value,
                        )
                    ),
                )
            ).all()
        )

    # ----------------------------------------------------------------- writes

    def create_subtasks_from_document(
        self,
        turn_id: str,
        document: Any,
        assignments: list[AssignmentDecision],
    ) -> list[SubtaskRead]:
        turn = self.require_turn(turn_id)
        existing = self.session.scalar(
            select(SubtaskRow.id).where(SubtaskRow.turn_id == turn_id).limit(1)
        )
        if existing is not None:
            raise ValueError(f"turn {turn_id} already has persisted subtasks")
        assigned = {item.key: item for item in assignments}
        rows: dict[str, SubtaskRow] = {}
        for spec in document.subtasks:
            decision = assigned.get(spec.key)
            if decision is None:
                raise ValueError(f"missing assignment decision for subtask {spec.key}")
            row = SubtaskRow(
                turn_id=turn_id,
                key=spec.key,
                title=spec.title,
                objective=spec.objective,
                required_capabilities_json=canonical_json_dumps(
                    [capability.value for capability in spec.required_capabilities]
                ),
                proposed_provider=spec.proposed_provider.value,
                assigned_provider=decision.assigned_provider.value,
                route_reason_code=decision.route_reason_code.value,
                route_reason=decision.route_reason,
                allowed_tools_json=canonical_json_dumps(list(decision.allowed_tools)),
                expected_output=spec.expected_output,
                required=spec.required,
            )
            self.session.add(row)
            rows[spec.key] = row
        self.session.flush()
        for spec in document.subtasks:
            row = rows[spec.key]
            for dependency_key in spec.dependency_keys:
                dependency = rows.get(dependency_key)
                if dependency is None:  # pragma: no cover - graph validation upstream
                    raise ValueError(f"unknown dependency key {dependency_key}")
                self.session.add(
                    SubtaskDependencyRow(
                        subtask_id=row.id, dependency_subtask_id=dependency.id
                    )
                )
        self.session.flush()
        for row in rows.values():
            self._append_event(
                turn.conversation_id,
                turn_id,
                row.id,
                "subtask.assigned",
                {
                    "subtask_id": row.id,
                    "key": row.key,
                    "turn_id": turn_id,
                    "assigned_provider": row.assigned_provider,
                    "route_reason_code": row.route_reason_code,
                    "route_reason": row.route_reason,
                    "required": row.required,
                },
            )
        return [self._subtask_read(row) for row in rows.values()]

    def transition_subtask(
        self,
        subtask_id: str,
        target: SubtaskStatus,
        *,
        expected: set[SubtaskStatus],
        reason: str,
    ) -> SubtaskRead:
        row = self.require_subtask(subtask_id)
        updated = self.session.execute(
            update(SubtaskRow)
            .where(
                SubtaskRow.id == subtask_id,
                SubtaskRow.status.in_([item.value for item in expected]),
            )
            .values(
                status=target.value,
                status_version=SubtaskRow.status_version + 1,
            )
            .returning(SubtaskRow.id)
        ).scalar_one_or_none()
        if updated is None:
            self.session.rollback()
            raise SubtaskTransitionError(
                f"illegal subtask transition for {subtask_id}: "
                f"{row.status} -> {target.value}"
            )
        turn = self.require_turn(row.turn_id)
        self._append_event(
            turn.conversation_id,
            row.turn_id,
            subtask_id,
            _EVENT_NAME_BY_STATUS[target],
            {
                "subtask_id": subtask_id,
                "key": row.key,
                "turn_id": row.turn_id,
                "status": target.value,
                "reason": reason,
            },
        )
        self.session.flush()
        return self.get_subtask(subtask_id)  # type: ignore[return-value]

    def set_result_summary(self, subtask_id: str, summary: str) -> None:
        self.session.execute(
            update(SubtaskRow)
            .where(SubtaskRow.id == subtask_id)
            .values(result_summary=summary)
        )
        self.session.flush()

    def create_attempt(
        self,
        subtask_id: str,
        *,
        provider: str,
        model: str,
        idempotency_key: str,
        worker_id: str | None = None,
    ) -> SubtaskAttemptRead:
        existing = self.session.scalar(
            select(SubtaskAttemptRow).where(
                SubtaskAttemptRow.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return self._attempt_read(existing)
        subtask = self.require_subtask(subtask_id)
        del subtask  # existence guard only
        last_attempt = self.session.scalar(
            select(SubtaskAttemptRow.attempt)
            .where(SubtaskAttemptRow.subtask_id == subtask_id)
            .order_by(SubtaskAttemptRow.attempt.desc())
            .limit(1)
        )
        row = SubtaskAttemptRow(
            subtask_id=subtask_id,
            attempt=(last_attempt or 0) + 1,
            provider=provider,
            model=model,
            idempotency_key=idempotency_key,
            status="running",
            worker_id=worker_id,
            started_at=datetime.now(timezone.utc),
        )
        self.session.add(row)
        self.session.flush()
        return self._attempt_read(row)

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        error_code: str | None = None,
    ) -> SubtaskAttemptRead:
        row = self.session.get(SubtaskAttemptRow, attempt_id)
        if row is None:
            raise KeyError(attempt_id)
        row.status = status
        row.error_code = error_code
        row.finished_at = datetime.now(timezone.utc)
        self.session.flush()
        return self._attempt_read(row)

    def save_subtask_result(
        self, attempt_id: str, result: SubtaskResultDocument
    ) -> SubtaskResultRead:
        attempt = self.session.get(SubtaskAttemptRow, attempt_id)
        if attempt is None:
            raise KeyError(attempt_id)
        self.session.execute(
            SubtaskResultRow.__table__.delete().where(
                SubtaskResultRow.attempt_id == attempt_id
            )
        )
        row = SubtaskResultRow(
            attempt_id=attempt_id,
            subtask_id=attempt.subtask_id,
            status=result.status,
            summary=result.summary,
            claims_json=canonical_json_dumps(
                [claim.model_dump(mode="json") for claim in result.claims]
            ),
            evidence_refs_json=canonical_json_dumps(result.evidence_refs),
            inference_notes_json=canonical_json_dumps(result.inference_notes),
            unresolved_json=canonical_json_dumps(result.unresolved),
        )
        self.session.add(row)
        self.set_result_summary(attempt.subtask_id, result.summary)
        self.session.flush()
        return self._result_read(row)

    def latest_result(self, subtask_id: str) -> SubtaskResultRead | None:
        row = self.session.scalar(
            select(SubtaskResultRow)
            .where(SubtaskResultRow.subtask_id == subtask_id)
            .order_by(SubtaskResultRow.created_at.desc())
            .limit(1)
        )
        return self._result_read(row) if row else None

    def record_model_failure(self, payload: ModelFailureCreate) -> ModelFailureRead:
        turn = self.require_turn(payload.turn_id)
        row = ModelFailureRow(
            conversation_id=turn.conversation_id,
            turn_id=payload.turn_id,
            subtask_id=payload.subtask_id,
            stage=payload.stage.value,
            provider=payload.provider,
            model=payload.model,
            error_code=payload.error_code,
            detail=payload.detail,
        )
        self.session.add(row)
        self.session.flush()
        self._append_event(
            turn.conversation_id,
            payload.turn_id,
            payload.subtask_id,
            "model.failure.waiting_decision",
            {
                "failure_id": row.id,
                "turn_id": payload.turn_id,
                "subtask_id": payload.subtask_id,
                "stage": payload.stage.value,
                "provider": payload.provider,
                "model": payload.model,
                "error_code": payload.error_code,
            },
        )
        return self._failure_read(row)

    def resolve_model_failure(
        self,
        failure_id: str,
        *,
        decision: ModelFailureDecision,
        decided_by: str,
    ) -> ModelFailureRead:
        updated = self.session.execute(
            update(ModelFailureRow)
            .where(
                ModelFailureRow.id == failure_id,
                ModelFailureRow.status == ModelFailureStatus.WAITING_DECISION.value,
            )
            .values(
                status=ModelFailureStatus.RESOLVED.value,
                decision=decision.value,
                decided_by=decided_by,
                decided_at=datetime.now(timezone.utc),
            )
            .returning(ModelFailureRow.id)
        ).scalar_one_or_none()
        if updated is None:
            self.session.rollback()
            raise ModelFailureAlreadyResolved(failure_id)
        row = self.session.get(ModelFailureRow, failure_id)
        assert row is not None
        self._append_event(
            row.conversation_id,
            row.turn_id,
            row.subtask_id,
            "model.failure.resolved",
            {
                "failure_id": failure_id,
                "turn_id": row.turn_id,
                "subtask_id": row.subtask_id,
                "decision": decision.value,
            },
        )
        self.session.flush()
        return self._failure_read(row)

    def get_model_failure(self, failure_id: str) -> ModelFailureRead | None:
        row = self.session.get(ModelFailureRow, failure_id)
        return self._failure_read(row) if row else None

    def mark_turn_state(
        self,
        turn_id: str,
        target: str,
        *,
        expected: set[str],
    ) -> ConversationTurnRow:
        now = datetime.now(timezone.utc)
        values: dict[str, Any] = {"status": target}
        if target == "running":
            values["started_at"] = now
        if target in {
            "completed",
            "partial",
            "failed",
            "failed_retryable",
            "cancelled",
            "superseded",
        }:
            values["finished_at"] = now
        updated = self.session.execute(
            update(ConversationTurnRow)
            .where(
                ConversationTurnRow.id == turn_id,
                ConversationTurnRow.status.in_(expected),
            )
            .values(**values)
            .returning(ConversationTurnRow.id)
        ).scalar_one_or_none()
        if updated is None:
            row = self.require_turn(turn_id)
            self.session.rollback()
            raise SubtaskTransitionError(
                f"illegal turn transition for {turn_id}: {row.status} -> {target}"
            )
        self.session.flush()
        return self.require_turn(turn_id)

    def record_subtask_event(
        self,
        subtask_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        row = self.require_subtask(subtask_id)
        turn = self.require_turn(row.turn_id)
        self._append_event(turn.conversation_id, row.turn_id, subtask_id, event_type, payload)

    def record_turn_event(
        self,
        turn_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        turn = self.require_turn(turn_id)
        self._append_event(turn.conversation_id, turn_id, None, event_type, payload)

    def set_assigned_provider(self, subtask_id: str, provider: str) -> None:
        self.session.execute(
            update(SubtaskRow)
            .where(SubtaskRow.id == subtask_id)
            .values(assigned_provider=provider)
        )
        self.session.flush()

    def requeue_failed_dag_job(self, command_id: str) -> bool:
        claimed = self.session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.command_id == command_id,
                JobRunRow.status == "failed",
            )
            .values(status="pending_publish")
            .returning(JobRunRow.id)
        ).scalar_one_or_none()
        self.session.flush()
        return claimed is not None

    def _close_out_active_subtasks(
        self, turn_id: str, target: SubtaskStatus, reason: str
    ) -> int:
        """Move every non-terminal subtask of the turn to the target state."""
        closed = 0
        rows = self.session.scalars(
            select(SubtaskRow).where(SubtaskRow.turn_id == turn_id)
        ).all()
        for row in rows:
            if SubtaskStatus(row.status) in TERMINAL_SUBTASK_STATUSES:
                continue
            try:
                self.transition_subtask(
                    row.id,
                    target,
                    expected={
                        SubtaskStatus.PENDING_DEPENDENCY,
                        SubtaskStatus.QUEUED,
                        SubtaskStatus.RUNNING,
                        SubtaskStatus.WAITING_TOOL_APPROVAL,
                        SubtaskStatus.WAITING_MODEL_DECISION,
                    },
                    reason=reason,
                )
                closed += 1
            except SubtaskTransitionError:
                continue
        return closed

    def supersede_active_subtasks(self, turn_id: str, reason: str) -> int:
        return self._close_out_active_subtasks(
            turn_id, SubtaskStatus.SUPERSEDED, reason
        )

    def cancel_active_subtasks(self, turn_id: str, reason: str) -> int:
        return self._close_out_active_subtasks(
            turn_id, SubtaskStatus.CANCELLED, reason
        )

    # ------------------------------------------------------------- dag job lifecycle

    def claim_dag_job(
        self,
        command_id: str,
        worker_id: str,
        *,
        lease_seconds: int,
    ) -> Any:
        """Claim a DAG job without touching the legacy compat TaskRow."""
        from datetime import timedelta

        from secagent.services.job_service import JobLease, utcnow

        current = utcnow()
        expires = current + timedelta(seconds=lease_seconds)
        row = self.session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.command_id == command_id,
                JobRunRow.status.in_(("publishing", "queued")),
            )
            .values(
                status="running",
                worker_id=worker_id,
                lease_expires_at=expires,
                heartbeat_at=current,
                started_at=current,
            )
            .returning(
                JobRunRow.id,
                JobRunRow.task_id,
                JobRunRow.attempt,
            )
        ).one_or_none()
        if row is None:
            self.session.rollback()
            return None
        self.session.commit()
        return JobLease(row.id, row.task_id, worker_id, row.attempt, expires)

    def finish_dag_job(
        self, job_run_id: str, worker_id: str, status: str
    ) -> bool:
        from secagent.services.job_service import utcnow

        if status not in {"completed", "failed"}:
            raise ValueError("invalid final job status")
        current = utcnow()
        claimed = self.session.execute(
            update(JobRunRow)
            .where(
                JobRunRow.id == job_run_id,
                JobRunRow.worker_id == worker_id,
                JobRunRow.status.in_(("running", "pause_requested", "cancel_requested")),
                JobRunRow.lease_expires_at > current,
            )
            .values(status=status, finished_at=current, lease_expires_at=None)
            .returning(JobRunRow.id)
        ).scalar_one_or_none()
        if claimed is None:
            self.session.rollback()
            return False
        self.session.commit()
        return True

    def recover_expired_dag_jobs(self) -> int:
        """Expired running DAG jobs go back to pending_publish for republish.

        Jobs whose turn was superseded while they were running (a follow-up
        message created a newer plan version) are NOT republished: they would
        crash again on the same illegal transition. Such jobs are marked
        ``failed`` so the zombie loop ends.
        """
        from secagent.services.job_service import _as_utc, utcnow

        current = utcnow()
        rows = list(
            self.session.scalars(
                select(JobRunRow).where(
                    JobRunRow.job_kind.in_(
                        [
                            JobKind.TURN_DECOMPOSE.value,
                            JobKind.SUBTASK_EXECUTE.value,
                            JobKind.TURN_SYNTHESIZE.value,
                        ]
                    ),
                    JobRunRow.status.in_(
                        ("running", "pause_requested", "cancel_requested")
                    ),
                )
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
        recovered = 0
        for row in rows:
            if row.lease_expires_at is None or _as_utc(row.lease_expires_at) > current:
                continue
            self.session.refresh(row)
            if row.status not in ("running", "pause_requested", "cancel_requested"):
                continue
            if row.turn_id is not None:
                turn = self.session.get(ConversationTurnRow, row.turn_id)
                if turn is not None and turn.status in superseded_turns:
                    # A newer plan version owns this conversation; do not
                    # republish a stale DAG job that would crash again.
                    row.status = "failed"
                    row.worker_id = None
                    row.heartbeat_at = None
                    row.lease_expires_at = None
                    row.finished_at = current
                    recovered += 1
                    continue
            row.status = "pending_publish"
            row.worker_id = None
            row.heartbeat_at = None
            row.lease_expires_at = None
            recovered += 1
        self.session.commit()
        return recovered

    # ----------------------------------------------------------------- internals

    def _append_event(
        self,
        conversation_id: str,
        turn_id: str,
        subtask_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        encoded = encode_redacted_event_payload(payload)
        self.session.add(
            ConversationEventRow(
                conversation_id=conversation_id,
                turn_id=turn_id,
                subtask_id=subtask_id,
                event_type=event_type,
                payload_json=encoded,
            )
        )
        self.session.flush()

    @staticmethod
    def _subtask_read(row: SubtaskRow) -> SubtaskRead:
        return SubtaskRead(
            id=row.id,
            turn_id=row.turn_id,
            key=row.key,
            title=row.title,
            objective=row.objective,
            required_capabilities=json.loads(row.required_capabilities_json),
            proposed_provider=row.proposed_provider,
            assigned_provider=row.assigned_provider,
            route_reason_code=row.route_reason_code,
            route_reason=row.route_reason,
            allowed_tools=json.loads(row.allowed_tools_json),
            expected_output=row.expected_output,
            required=row.required,
            status=SubtaskStatus(row.status),
            status_version=row.status_version,
            result_summary=row.result_summary,
        )

    @staticmethod
    def _attempt_read(row: SubtaskAttemptRow) -> SubtaskAttemptRead:
        return SubtaskAttemptRead(
            id=row.id,
            subtask_id=row.subtask_id,
            attempt=row.attempt,
            provider=row.provider,
            model=row.model,
            idempotency_key=row.idempotency_key,
            status=row.status,
            error_code=row.error_code,
        )

    @staticmethod
    def _result_read(row: SubtaskResultRow) -> SubtaskResultRead:
        return SubtaskResultRead(
            id=row.id,
            attempt_id=row.attempt_id,
            subtask_id=row.subtask_id,
            status=row.status,
            summary=row.summary,
            claims=[
                ClaimDocument.model_validate(item)
                for item in json.loads(row.claims_json)
            ],
            evidence_refs=json.loads(row.evidence_refs_json),
            inference_notes=json.loads(row.inference_notes_json),
            unresolved=json.loads(row.unresolved_json),
        )

    @staticmethod
    def _failure_read(row: ModelFailureRow) -> ModelFailureRead:
        return ModelFailureRead(
            id=row.id,
            conversation_id=row.conversation_id,
            turn_id=row.turn_id,
            subtask_id=row.subtask_id,
            stage=ModelFailureStage(row.stage),
            provider=row.provider,
            model=row.model,
            error_code=row.error_code,
            detail=row.detail,
            status=ModelFailureStatus(row.status),
            decision=ModelFailureDecision(row.decision) if row.decision else None,
            decided_by=row.decided_by,
        )
