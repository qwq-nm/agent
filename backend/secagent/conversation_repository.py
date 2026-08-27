from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, TypeVar

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from secagent.api.errors import ForbiddenResource
from secagent.conversation_domain import (
    AttachmentMetadataCreate,
    AttachmentRead,
    ConversationCreate,
    ConversationEventCreate,
    ConversationEventRead,
    ConversationMessageRead,
    ConversationMessageWrite,
    ConversationPatch,
    ConversationRead,
    ConversationSettings,
    ConversationStatus,
    ConversationTurnCreate,
    ConversationTurnRead,
    TurnBudgetSnapshot,
    canonical_json_dumps,
    canonical_json_loads,
)
from secagent.db_models import (
    ConversationEventRow,
    ConversationMessageRow,
    ConversationRow,
    ConversationTurnRow,
    MessageAttachmentRow,
    TaskRow,
)
from secagent.domain import UserRole

if TYPE_CHECKING:
    from secagent.auth.dependencies import AuthenticatedUser


class ConversationInvariantError(ValueError):
    """A child reference violates a conversation relationship invariant."""


class MessageIdempotencyConflict(RuntimeError):
    """An idempotency key was reused for a different immutable message."""


@dataclass(frozen=True)
class MessageAppendResult:
    message: ConversationMessageRead
    created: bool


T = TypeVar("T")


class ConversationRepository:
    """Owner-aware persistence boundary for conversational orchestration."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def create_conversation(
        self,
        actor: AuthenticatedUser,
        payload: ConversationCreate,
        *,
        commit: bool = True,
    ) -> ConversationRead:
        def write() -> ConversationRead:
            row = ConversationRow(
                owner_id=actor.id,
                title=payload.title,
                settings_json=canonical_json_dumps(payload.settings),
            )
            self.session.add(row)
            self.session.flush()
            return self._conversation_read(row)

        return self._write(write, commit=commit)

    def get_conversation(
        self, actor: AuthenticatedUser, conversation_id: str
    ) -> ConversationRead | None:
        row = self.session.get(
            ConversationRow, conversation_id, populate_existing=True
        )
        if row is None:
            return None
        self._authorize(actor, row)
        return self._conversation_read(row)

    def list_conversations(
        self, actor: AuthenticatedUser, *, limit: int = 100
    ) -> list[ConversationRead]:
        self._validate_limit(limit, maximum=200)
        statement = select(ConversationRow)
        if actor.role is not UserRole.ADMIN:
            statement = statement.where(ConversationRow.owner_id == actor.id)
        rows = self.session.scalars(
            statement.order_by(
                ConversationRow.created_at.desc(), ConversationRow.id.desc()
            ).limit(limit)
        ).all()
        return [self._conversation_read(row) for row in rows]

    def patch_conversation(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        payload: ConversationPatch,
        *,
        commit: bool = True,
    ) -> ConversationRead:
        def write() -> ConversationRead:
            row = self._require_conversation(actor, conversation_id)
            if "title" in payload.model_fields_set:
                row.title = payload.title
            if "settings" in payload.model_fields_set:
                row.settings_json = canonical_json_dumps(payload.settings)
            self.session.flush()
            return self._conversation_read(row)

        return self._write(write, commit=commit)

    def archive_conversation(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        *,
        commit: bool = True,
    ) -> ConversationRead:
        def write() -> ConversationRead:
            row = self._require_conversation(actor, conversation_id)
            row.status = ConversationStatus.ARCHIVED.value
            self.session.flush()
            return self._conversation_read(row)

        return self._write(write, commit=commit)

    def add_message(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        payload: ConversationMessageWrite,
        *,
        commit: bool = True,
    ) -> MessageAppendResult:
        def write() -> MessageAppendResult:
            self._lock_conversation(actor, conversation_id)
            if payload.idempotency_key is not None:
                existing = self.session.scalar(
                    select(ConversationMessageRow).where(
                        ConversationMessageRow.conversation_id == conversation_id,
                        ConversationMessageRow.idempotency_key
                        == payload.idempotency_key,
                    )
                )
                if existing is not None:
                    identity = (existing.role, existing.kind, existing.content)
                    requested = (
                        payload.role.value,
                        payload.kind.value,
                        payload.content,
                    )
                    if identity != requested:
                        raise MessageIdempotencyConflict(
                            "idempotency key belongs to a different message"
                        )
                    return MessageAppendResult(
                        message=self._message_read(existing), created=False
                    )

            if payload.turn_id is not None:
                self._require_turn(conversation_id, payload.turn_id)
            sequence = self._allocate_counter(
                conversation_id,
                ConversationRow.next_message_sequence,
                "next_message_sequence",
            )
            row = ConversationMessageRow(
                conversation_id=conversation_id,
                sequence=sequence,
                role=payload.role.value,
                kind=payload.kind.value,
                content=payload.content,
                status=payload.status.value,
                turn_id=payload.turn_id,
                idempotency_key=payload.idempotency_key,
            )
            self.session.add(row)
            self.session.flush()
            return MessageAppendResult(message=self._message_read(row), created=True)

        return self._write(write, commit=commit)

    def get_message(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        message_id: str,
    ) -> ConversationMessageRead | None:
        self._require_conversation(actor, conversation_id)
        row = self.session.scalar(
            select(ConversationMessageRow).where(
                ConversationMessageRow.id == message_id,
                ConversationMessageRow.conversation_id == conversation_id,
            )
        )
        return self._message_read(row) if row is not None else None

    def list_messages(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[ConversationMessageRead]:
        self._validate_after(after_sequence, "after_sequence")
        self._validate_limit(limit, maximum=500)
        self._require_conversation(actor, conversation_id)
        rows = self.session.scalars(
            select(ConversationMessageRow)
            .where(
                ConversationMessageRow.conversation_id == conversation_id,
                ConversationMessageRow.sequence > after_sequence,
            )
            .order_by(ConversationMessageRow.sequence.asc())
            .limit(limit)
        ).all()
        return [self._message_read(row) for row in rows]

    def add_attachment(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        message_id: str,
        payload: AttachmentMetadataCreate,
        *,
        commit: bool = True,
    ) -> AttachmentRead:
        def write() -> AttachmentRead:
            self._require_conversation(actor, conversation_id)
            self._require_message(conversation_id, message_id)
            row = MessageAttachmentRow(
                message_id=message_id, **payload.model_dump(mode="json")
            )
            self.session.add(row)
            self.session.flush()
            return self._attachment_read(row)

        return self._write(write, commit=commit)

    def list_attachments(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        message_id: str,
        *,
        limit: int = 100,
    ) -> list[AttachmentRead]:
        self._validate_limit(limit, maximum=500)
        self._require_conversation(actor, conversation_id)
        self._require_message(conversation_id, message_id)
        rows = self.session.scalars(
            select(MessageAttachmentRow)
            .where(MessageAttachmentRow.message_id == message_id)
            .order_by(
                MessageAttachmentRow.created_at.asc(), MessageAttachmentRow.id.asc()
            )
            .limit(limit)
        ).all()
        return [self._attachment_read(row) for row in rows]

    def create_turn(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        payload: ConversationTurnCreate,
        *,
        commit: bool = True,
    ) -> ConversationTurnRead:
        def write() -> ConversationTurnRead:
            conversation = self._lock_conversation(actor, conversation_id)
            trigger = self._require_message(
                conversation_id, payload.trigger_message_id
            )
            if trigger.turn_id is not None:
                raise ConversationInvariantError(
                    "trigger message is already linked to a turn"
                )
            if payload.replan_from_turn_id is not None:
                self._require_turn(conversation_id, payload.replan_from_turn_id)
            if payload.task_id is not None:
                task = self.session.get(TaskRow, payload.task_id)
                if (
                    task is None
                    or task.owner_id is None
                    or task.owner_id != conversation.owner_id
                ):
                    raise ConversationInvariantError(
                        "task must exist and have the conversation owner"
                    )
            plan_version = self._allocate_counter(
                conversation_id,
                ConversationRow.next_turn_plan_version,
                "next_turn_plan_version",
            )
            row = ConversationTurnRow(
                conversation_id=conversation_id,
                trigger_message_id=payload.trigger_message_id,
                task_id=payload.task_id,
                plan_version=plan_version,
                budget_json=canonical_json_dumps(payload.budget),
                replan_from_turn_id=payload.replan_from_turn_id,
            )
            self.session.add(row)
            self.session.flush()
            trigger.turn_id = row.id
            conversation.active_turn_id = row.id
            self.session.flush()
            return self._turn_read(row)

        return self._write(write, commit=commit)

    def get_turn(
        self, actor: AuthenticatedUser, conversation_id: str, turn_id: str
    ) -> ConversationTurnRead | None:
        self._require_conversation(actor, conversation_id)
        row = self.session.scalar(
            select(ConversationTurnRow).where(
                ConversationTurnRow.id == turn_id,
                ConversationTurnRow.conversation_id == conversation_id,
            )
        )
        return self._turn_read(row) if row is not None else None

    def list_turns(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        *,
        after_plan_version: int = 0,
        limit: int = 100,
    ) -> list[ConversationTurnRead]:
        self._validate_after(after_plan_version, "after_plan_version")
        self._validate_limit(limit, maximum=500)
        self._require_conversation(actor, conversation_id)
        rows = self.session.scalars(
            select(ConversationTurnRow)
            .where(
                ConversationTurnRow.conversation_id == conversation_id,
                ConversationTurnRow.plan_version > after_plan_version,
            )
            .order_by(ConversationTurnRow.plan_version.asc())
            .limit(limit)
        ).all()
        return [self._turn_read(row) for row in rows]

    def set_active_turn(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        turn_id: str,
        *,
        commit: bool = True,
    ) -> ConversationRead:
        def write() -> ConversationRead:
            conversation = self._lock_conversation(actor, conversation_id)
            turn = self._require_turn(conversation_id, turn_id)
            highest_version = self.session.scalar(
                select(func.max(ConversationTurnRow.plan_version)).where(
                    ConversationTurnRow.conversation_id == conversation_id
                )
            )
            if turn.plan_version != highest_version:
                raise ConversationInvariantError(
                    "active turn must be the highest plan version"
                )
            conversation.active_turn_id = turn.id
            self.session.flush()
            return self._conversation_read(conversation)

        return self._write(write, commit=commit)

    def clear_active_turn(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        *,
        expected_turn_id: str,
        commit: bool = True,
    ) -> ConversationRead:
        def write() -> ConversationRead:
            self._lock_conversation(actor, conversation_id)
            self._require_turn(conversation_id, expected_turn_id)
            self.session.execute(
                update(ConversationRow)
                .where(
                    ConversationRow.id == conversation_id,
                    ConversationRow.active_turn_id == expected_turn_id,
                )
                .values(active_turn_id=None)
            )
            self.session.flush()
            row = self.session.get(
                ConversationRow, conversation_id, populate_existing=True
            )
            if row is None:  # pragma: no cover - protected by the write lock
                raise KeyError(conversation_id)
            return self._conversation_read(row)

        return self._write(write, commit=commit)

    def append_event(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        payload: ConversationEventCreate,
        *,
        commit: bool = True,
    ) -> ConversationEventRead:
        def write() -> ConversationEventRead:
            self._require_conversation(actor, conversation_id)
            if payload.turn_id is not None:
                self._require_turn(conversation_id, payload.turn_id)
            row = ConversationEventRow(
                conversation_id=conversation_id,
                event_type=payload.event_type,
                payload_json=canonical_json_dumps(payload.payload),
                turn_id=payload.turn_id,
                subtask_id=payload.subtask_id,
            )
            self.session.add(row)
            self.session.flush()
            return self._event_read(row)

        return self._write(write, commit=commit)

    def events_after(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        *,
        cursor: int = 0,
        limit: int = 100,
    ) -> list[ConversationEventRead]:
        self._validate_after(cursor, "cursor")
        self._validate_limit(limit, maximum=1_000)
        self._require_conversation(actor, conversation_id)
        rows = self.session.scalars(
            select(ConversationEventRow)
            .where(
                ConversationEventRow.conversation_id == conversation_id,
                ConversationEventRow.id > cursor,
            )
            .order_by(ConversationEventRow.id.asc())
            .limit(limit)
        ).all()
        return [self._event_read(row) for row in rows]

    def _write(self, operation: Callable[[], T], *, commit: bool) -> T:
        nested = self.session.begin_nested()
        try:
            result = operation()
            self.session.flush()
        except Exception:
            nested.rollback()
            if commit:
                self.session.rollback()
            raise
        if commit:
            nested.commit()
            self.session.commit()
        return result

    def _lock_conversation(
        self, actor: AuthenticatedUser, conversation_id: str
    ) -> ConversationRow:
        statement = (
            update(ConversationRow)
            .where(ConversationRow.id == conversation_id)
            .values(
                id=ConversationRow.id,
                updated_at=ConversationRow.updated_at,
            )
        )
        if actor.role is not UserRole.ADMIN:
            statement = statement.where(ConversationRow.owner_id == actor.id)
        found_id = self.session.scalar(statement.returning(ConversationRow.id))
        if found_id is None:
            row = self.session.get(
                ConversationRow, conversation_id, populate_existing=True
            )
            if row is None:
                raise KeyError(conversation_id)
            raise ForbiddenResource("conversation")
        row = self.session.get(
            ConversationRow, conversation_id, populate_existing=True
        )
        if row is None:  # pragma: no cover - returned by the UPDATE above
            raise KeyError(conversation_id)
        return row

    def _allocate_counter(
        self,
        conversation_id: str,
        counter_column,
        counter_name: str,
    ) -> int:
        next_value = self.session.scalar(
            update(ConversationRow)
            .where(ConversationRow.id == conversation_id)
            .values({counter_name: counter_column + 1})
            .returning(counter_column)
        )
        if next_value is None:  # pragma: no cover - protected by the write lock
            raise KeyError(conversation_id)
        return int(next_value) - 1

    def _require_conversation(
        self, actor: AuthenticatedUser, conversation_id: str
    ) -> ConversationRow:
        row = self.session.get(
            ConversationRow, conversation_id, populate_existing=True
        )
        if row is None:
            raise KeyError(conversation_id)
        self._authorize(actor, row)
        return row

    @staticmethod
    def _authorize(actor: AuthenticatedUser, row: ConversationRow) -> None:
        if actor.role is not UserRole.ADMIN and actor.id != row.owner_id:
            raise ForbiddenResource("conversation")

    def _require_message(
        self, conversation_id: str, message_id: str
    ) -> ConversationMessageRow:
        row = self.session.scalar(
            select(ConversationMessageRow).where(
                ConversationMessageRow.id == message_id,
                ConversationMessageRow.conversation_id == conversation_id,
            )
        )
        if row is None:
            raise ConversationInvariantError(
                "message must belong to the conversation"
            )
        return row

    def _require_turn(
        self, conversation_id: str, turn_id: str
    ) -> ConversationTurnRow:
        row = self.session.scalar(
            select(ConversationTurnRow).where(
                ConversationTurnRow.id == turn_id,
                ConversationTurnRow.conversation_id == conversation_id,
            )
        )
        if row is None:
            raise ConversationInvariantError("turn must belong to the conversation")
        return row

    @staticmethod
    def _validate_after(value: int, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")

    @staticmethod
    def _validate_limit(value: int, *, maximum: int) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > maximum
        ):
            raise ValueError(f"limit must be an integer from 1 through {maximum}")

    @staticmethod
    def _conversation_read(row: ConversationRow) -> ConversationRead:
        return ConversationRead(
            id=row.id,
            owner_id=row.owner_id,
            title=row.title,
            status=row.status,
            settings=canonical_json_loads(row.settings_json, ConversationSettings),
            active_turn_id=row.active_turn_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _message_read(row: ConversationMessageRow) -> ConversationMessageRead:
        return ConversationMessageRead.model_validate(row, from_attributes=True)

    @staticmethod
    def _attachment_read(row: MessageAttachmentRow) -> AttachmentRead:
        return AttachmentRead.model_validate(row, from_attributes=True)

    @staticmethod
    def _turn_read(row: ConversationTurnRow) -> ConversationTurnRead:
        return ConversationTurnRead(
            id=row.id,
            conversation_id=row.conversation_id,
            trigger_message_id=row.trigger_message_id,
            task_id=row.task_id,
            plan_version=row.plan_version,
            status=row.status,
            budget=canonical_json_loads(row.budget_json, TurnBudgetSnapshot),
            replan_from_turn_id=row.replan_from_turn_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )

    @staticmethod
    def _event_read(row: ConversationEventRow) -> ConversationEventRead:
        return ConversationEventRead(
            cursor=row.id,
            conversation_id=row.conversation_id,
            event_type=row.event_type,
            payload=canonical_json_loads(row.payload_json, dict[str, object]),
            turn_id=row.turn_id,
            subtask_id=row.subtask_id,
            created_at=row.created_at,
        )
