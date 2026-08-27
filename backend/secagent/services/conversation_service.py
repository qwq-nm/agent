from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Sequence

from fastapi import UploadFile
from pydantic import TypeAdapter
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from secagent.config import Settings
from secagent.conversation_domain import (
    AttachmentRead,
    ConversationCreate,
    ConversationDetailRead,
    ConversationMessageKind,
    ConversationMessageRead,
    ConversationMessageRole,
    ConversationMessageStatus,
    ConversationMessageWrite,
    ConversationPatch,
    ConversationRead,
    ConversationStatus,
    ConversationTurnCreate,
    ConversationTurnRead,
    IdempotencyKey,
    MessageSendRead,
    MessageSubmission,
    MessageWithAttachments,
    TurnBudgetSnapshot,
)
from secagent.conversation_repository import (
    ConversationRepository,
    ConversationRepositoryStateError,
    MessageIdempotencyConflict,
)
from secagent.domain import TaskCreate
from secagent.repository import TaskRepository
from secagent.services.audit import AuditService
from secagent.services.conversation_events import ConversationEventService
from secagent.services.conversation_storage import (
    ConversationStorageService,
    StagedAttachmentBatch,
)

if TYPE_CHECKING:
    from secagent.auth.dependencies import AuthenticatedUser


_SAFE_AUTHORIZATION_FALLBACK = "No external target authorization granted."


class ArchivedConversationError(RuntimeError):
    """A new message cannot be added to an archived conversation."""


class AttachmentIdempotencyConflict(RuntimeError):
    """An idempotency key was reused with a different attachment multiset."""


class ConversationReplayCorruptState(RuntimeError):
    """A persisted message replay is missing its required Turn."""


class ConversationCommitOutcomeUnknown(RuntimeError):
    """COMMIT outcome is uncertain and filesystem reconciliation is required."""


@dataclass(frozen=True)
class _ReplayState:
    message: ConversationMessageRead
    attachments: list[AttachmentRead]
    turn: ConversationTurnRead


class ConversationService:
    """Request-scoped orchestration boundary for conversation persistence."""

    def __init__(
        self,
        *,
        conversation_repository: ConversationRepository,
        task_repository: TaskRepository,
        event_writer: ConversationEventService,
        audit_writer: AuditService,
        storage: ConversationStorageService,
        settings: Settings,
        session_factory: Callable[[], Session],
    ) -> None:
        session = conversation_repository.session
        if any(
            writer.session is not session
            for writer in (task_repository, event_writer, audit_writer)
        ):
            raise ConversationRepositoryStateError(
                "all service writers must share the same writer Session"
            )
        self.session = session
        self.conversation_repository = conversation_repository
        self.task_repository = task_repository
        self.event_writer = event_writer
        self.audit_writer = audit_writer
        self.storage = storage
        self.settings = settings
        self.session_factory = session_factory

    def create(
        self, actor: AuthenticatedUser, payload: ConversationCreate
    ) -> ConversationRead:
        self._require_clean_writer()
        try:
            self.session.begin()
            conversation = self.conversation_repository.create_conversation(
                actor, payload, commit=False
            )
            self.audit_writer.record(
                actor.id,
                "conversation.create",
                "conversation",
                conversation.id,
                "success",
                {"conversation_id": conversation.id},
            )
            self.session.commit()
            return conversation
        except Exception:
            self._rollback_if_active()
            raise

    def list(
        self, actor: AuthenticatedUser, *, limit: int = 100
    ) -> list[ConversationRead]:
        with self.session_factory() as session:
            return ConversationRepository(session).list_conversations(
                actor, limit=limit
            )

    def get_detail(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        *,
        after_sequence: int = 0,
        message_limit: int = 100,
        after_plan_version: int = 0,
        turn_limit: int = 100,
    ) -> ConversationDetailRead | None:
        with self.session_factory() as session:
            repository = ConversationRepository(session)
            conversation = repository.get_conversation(actor, conversation_id)
            if conversation is None:
                return None
            messages = repository.list_messages(
                actor,
                conversation_id,
                after_sequence=after_sequence,
                limit=message_limit,
            )
            items = [
                MessageWithAttachments(
                    message=message,
                    attachments=repository.list_attachments(
                        actor, conversation_id, message.id
                    ),
                )
                for message in messages
            ]
            turns = repository.list_turns(
                actor,
                conversation_id,
                after_plan_version=after_plan_version,
                limit=turn_limit,
            )
            active_turn = (
                repository.get_turn(
                    actor, conversation_id, conversation.active_turn_id
                )
                if conversation.active_turn_id is not None
                else None
            )
            return ConversationDetailRead(
                conversation=conversation,
                messages=items,
                turns=turns,
                active_turn=active_turn,
            )

    def patch(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        payload: ConversationPatch,
    ) -> ConversationRead:
        self._require_clean_writer()
        try:
            self.session.begin()
            conversation = self.conversation_repository.patch_conversation(
                actor, conversation_id, payload, commit=False
            )
            self.audit_writer.record(
                actor.id,
                "conversation.patch",
                "conversation",
                conversation_id,
                "success",
                {
                    "conversation_id": conversation_id,
                    "field_count": len(payload.model_fields_set),
                },
            )
            self.session.commit()
            return conversation
        except Exception:
            self._rollback_if_active()
            raise

    def archive(
        self, actor: AuthenticatedUser, conversation_id: str
    ) -> ConversationRead:
        self._require_clean_writer()
        try:
            self.session.begin()
            conversation = self.conversation_repository.archive_conversation(
                actor, conversation_id, commit=False
            )
            self.audit_writer.record(
                actor.id,
                "conversation.archive",
                "conversation",
                conversation_id,
                "success",
                {"conversation_id": conversation_id},
            )
            self.session.commit()
            return conversation
        except Exception:
            self._rollback_if_active()
            raise

    async def send_message(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        submission: MessageSubmission,
        uploads: Sequence[UploadFile],
        idempotency_key: str,
    ) -> MessageSendRead:
        self._require_clean_writer()
        validated_key = TypeAdapter(IdempotencyKey).validate_python(
            idempotency_key
        )
        preflight = self._preflight(
            actor, conversation_id, submission.content, validated_key
        )
        self._require_clean_writer()
        batch = await self.storage.stage_many(uploads, submission.relative_paths)
        try:
            if preflight is not None:
                self._require_same_attachments(batch, preflight.attachments)
                return self._message_send_read(
                    preflight.message,
                    preflight.attachments,
                    preflight.turn,
                    replayed=True,
                )
            self._require_clean_writer()
            return self._compose_new_message(
                actor,
                conversation_id,
                submission,
                batch,
                validated_key,
            )
        finally:
            self.storage.cleanup_staged(batch)

    def _preflight(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        content: str,
        idempotency_key: str,
    ) -> _ReplayState | None:
        with self.session_factory() as session:
            repository = ConversationRepository(session)
            conversation = repository.get_conversation(actor, conversation_id)
            if conversation is None:
                raise KeyError(conversation_id)
            existing = repository.get_message_by_idempotency_key(
                actor, conversation_id, idempotency_key
            )
            if existing is None:
                if conversation.status is ConversationStatus.ARCHIVED:
                    raise ArchivedConversationError(
                        "archived conversations reject new messages"
                    )
                return None
            if (
                existing.content != content
                or existing.role is not ConversationMessageRole.USER
                or existing.kind is not ConversationMessageKind.USER_TEXT
            ):
                raise MessageIdempotencyConflict(
                    "idempotency key belongs to a different message"
                )
            return self._load_replay(repository, actor, conversation_id, existing)

    def _compose_new_message(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        submission: MessageSubmission,
        batch: StagedAttachmentBatch,
        idempotency_key: str,
    ) -> MessageSendRead:
        published_message_id: str | None = None
        preserve_published = False
        committed = False
        response_parts: tuple[
            ConversationMessageRead,
            list[AttachmentRead],
            ConversationTurnRead,
        ] | None = None
        try:
            self.session.begin()
            appended = self.conversation_repository.add_message(
                actor,
                conversation_id,
                ConversationMessageWrite(
                    role=ConversationMessageRole.USER,
                    kind=ConversationMessageKind.USER_TEXT,
                    content=submission.content,
                    status=ConversationMessageStatus.COMPLETED,
                    idempotency_key=idempotency_key,
                ),
                commit=False,
            )
            if not appended.created:
                replay = self._load_replay(
                    self.conversation_repository,
                    actor,
                    conversation_id,
                    appended.message,
                )
                self._require_same_attachments(batch, replay.attachments)
                self._commit_with_outcome_tracking()
                committed = True
                replay = self._refresh_committed_message(
                    actor, conversation_id, replay.message.id
                )
                return self._message_send_read(
                    replay.message,
                    replay.attachments,
                    replay.turn,
                    replayed=True,
                )

            locked = self.conversation_repository.get_conversation(
                actor, conversation_id
            )
            if locked is None:  # pragma: no cover - protected by append lock
                raise KeyError(conversation_id)
            if locked.status is ConversationStatus.ARCHIVED:
                raise ArchivedConversationError(
                    "archived conversations reject new messages"
                )
            budget = self._turn_budget_snapshot()
            published_message_id = appended.message.id
            metadata = self.storage.publish(
                batch, conversation_id, appended.message.id
            )
            attachments = [
                self.conversation_repository.add_attachment(
                    actor,
                    conversation_id,
                    appended.message.id,
                    item,
                    commit=False,
                )
                for item in metadata
            ]

            collapsed = re.sub(r"\s+", " ", submission.content).strip()
            scope = locked.settings.authorization_scope
            if not 3 <= len(scope) <= 2000:
                scope = _SAFE_AUTHORIZATION_FALLBACK
            task = self.task_repository.create_task(
                TaskCreate(
                    goal=("Conversation turn: " + collapsed)[:4000],
                    authorization_scope=scope,
                    safety_mode=locked.settings.safety_mode,
                ),
                owner_id=locked.owner_id,
                commit=False,
            )
            self.task_repository.configure_task_budget(
                task.id,
                max_model_calls=self.settings.max_model_calls_per_task,
                max_input_tokens=self.settings.max_input_tokens_per_task,
                max_output_tokens=self.settings.max_output_tokens_per_task,
                max_steps=self.settings.max_steps_per_task,
            )
            turn = self.conversation_repository.create_turn(
                actor,
                conversation_id,
                ConversationTurnCreate(
                    trigger_message_id=appended.message.id,
                    task_id=task.id,
                    budget=budget,
                ),
                commit=False,
            )
            message = self.conversation_repository.get_message(
                actor, conversation_id, appended.message.id
            )
            if message is None:  # pragma: no cover - inserted in this transaction
                raise KeyError(appended.message.id)
            if message.turn_id != turn.id:
                message = message.model_copy(update={"turn_id": turn.id})

            if appended.message.sequence == 1 and locked.title == "新对话":
                self.conversation_repository.patch_conversation(
                    actor,
                    conversation_id,
                    ConversationPatch(title=collapsed[:40]),
                    commit=False,
                )
            self.event_writer.append_message_created(
                actor,
                conversation_id=conversation_id,
                message_id=message.id,
                turn_id=turn.id,
                task_id=task.id,
                sequence=message.sequence,
                plan_version=turn.plan_version,
                attachment_count=len(attachments),
            )
            self.audit_writer.record(
                actor.id,
                "conversation.message.send",
                "conversation",
                conversation_id,
                "success",
                {
                    "conversation_id": conversation_id,
                    "message_id": message.id,
                    "turn_id": turn.id,
                    "task_id": task.id,
                    "sequence": message.sequence,
                    "plan_version": turn.plan_version,
                    "attachment_count": len(attachments),
                },
            )
            response_parts = (message, attachments, turn)
            try:
                self.session.commit()
            except Exception as exc:
                if self._commit_explicitly_not_applied(exc):
                    self.session.rollback()
                    raise
                preserve_published = True
                self.session.invalidate()
                raise ConversationCommitOutcomeUnknown(
                    "COMMIT outcome is uncertain; reconciliation is required"
                ) from exc
            committed = True
        except Exception:
            if not committed and self.session.in_transaction():
                self.session.rollback()
            if (
                not committed
                and not preserve_published
                and published_message_id is not None
            ):
                self.storage.cleanup_published_message(
                    conversation_id, published_message_id
                )
            raise

        if response_parts is None:  # pragma: no cover - guarded by successful commit
            raise RuntimeError("message response state was not composed")
        refreshed = self._refresh_committed_message(
            actor, conversation_id, response_parts[0].id
        )
        return self._message_send_read(
            refreshed.message,
            refreshed.attachments,
            refreshed.turn,
            replayed=False,
        )

    def _load_replay(
        self,
        repository: ConversationRepository,
        actor: AuthenticatedUser,
        conversation_id: str,
        message: ConversationMessageRead,
    ) -> _ReplayState:
        if message.turn_id is None:
            raise ConversationReplayCorruptState(
                "idempotent message is missing its Turn"
            )
        turn = repository.get_turn(actor, conversation_id, message.turn_id)
        if turn is None:
            raise ConversationReplayCorruptState(
                "idempotent message references a missing Turn"
            )
        attachments = repository.list_attachments(
            actor, conversation_id, message.id
        )
        return _ReplayState(message=message, attachments=attachments, turn=turn)

    def _refresh_committed_message(
        self,
        actor: AuthenticatedUser,
        conversation_id: str,
        message_id: str,
    ) -> _ReplayState:
        with self.session_factory() as session:
            repository = ConversationRepository(session)
            message = repository.get_message(actor, conversation_id, message_id)
            if message is None:
                raise ConversationReplayCorruptState(
                    "committed message could not be refreshed"
                )
            return self._load_replay(repository, actor, conversation_id, message)

    def _require_same_attachments(
        self,
        batch: StagedAttachmentBatch,
        persisted: Sequence[AttachmentRead],
    ) -> None:
        staged_counter = Counter(self.storage.attachment_identities(batch))
        persisted_counter = Counter(
            (
                item.original_name,
                item.relative_path,
                item.content_type,
                item.size_bytes,
                item.sha256,
            )
            for item in persisted
        )
        if staged_counter != persisted_counter:
            raise AttachmentIdempotencyConflict(
                "idempotency key belongs to a different attachment multiset"
            )

    def _turn_budget_snapshot(self) -> TurnBudgetSnapshot:
        return TurnBudgetSnapshot(
            max_subtasks=self.settings.max_subtasks_per_turn,
            max_model_calls_per_subtask=self.settings.max_model_calls_per_subtask,
            max_tool_calls_per_subtask=self.settings.max_tool_calls_per_subtask,
            timeout_seconds=self.settings.subtask_timeout_seconds,
            max_replans=self.settings.max_replans_per_turn,
            max_context_tokens=self.settings.max_conversation_context_tokens,
        )

    @staticmethod
    def _message_send_read(
        message: ConversationMessageRead,
        attachments: list[AttachmentRead],
        turn: ConversationTurnRead,
        *,
        replayed: bool,
    ) -> MessageSendRead:
        return MessageSendRead(
            message=message,
            attachments=attachments,
            turn=turn,
            replayed=replayed,
        )

    def _commit_with_outcome_tracking(self) -> None:
        try:
            self.session.commit()
        except Exception as exc:
            if self._commit_explicitly_not_applied(exc):
                self.session.rollback()
                raise
            self.session.invalidate()
            raise ConversationCommitOutcomeUnknown(
                "COMMIT outcome is uncertain; reconciliation is required"
            ) from exc

    def _commit_explicitly_not_applied(self, error: Exception) -> bool:
        if isinstance(error, DBAPIError) and error.connection_invalidated:
            return False
        return self.session.in_transaction()

    def _require_clean_writer(self) -> None:
        meaningfully_dirty = any(
            self.session.is_modified(row, include_collections=True)
            for row in self.session.dirty
        )
        if (
            self.session.in_transaction()
            or self.session.new
            or self.session.deleted
            or meaningfully_dirty
        ):
            raise ConversationRepositoryStateError(
                "a clean writer Session is required at service entry"
            )

    def _rollback_if_active(self) -> None:
        if self.session.in_transaction():
            self.session.rollback()
