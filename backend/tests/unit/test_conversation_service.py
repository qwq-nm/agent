import asyncio
import io
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi import UploadFile
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from secagent.api.errors import ForbiddenResource
from secagent.auth.dependencies import AuthenticatedUser
from secagent.config import Settings
from secagent.conversation_domain import (
    ConversationCreate,
    ConversationMessageWrite,
    ConversationPatch,
    ConversationSettings,
    ConversationTurnCreate,
    MessageSubmission,
    TurnBudgetSnapshot,
)
from secagent.conversation_repository import (
    ConversationRepository,
    ConversationRepositoryStateError,
    MessageIdempotencyConflict,
)
from secagent.db import Base, make_engine
from secagent.db_models import (
    AuditEventRow,
    ConversationEventRow,
    ConversationMessageRow,
    ConversationRow,
    ConversationTurnRow,
    JobRunRow,
    MessageAttachmentRow,
    TaskRow,
    UserRow,
)
from secagent.domain import SafetyMode, UserRole
from secagent.repository import TaskRepository
from secagent.services.audit import AuditService
from secagent.services.conversation_events import ConversationEventService
from secagent.services.conversation_service import (
    ArchivedConversationError,
    AttachmentIdempotencyConflict,
    ConversationCommitOutcomeUnknown,
    ConversationReplayCorruptState,
    ConversationService,
)
from secagent.services.conversation_storage import ConversationStorageService


def _uuid() -> str:
    return str(uuid4())


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


class ServiceEnvironment:
    def __init__(self, tmp_path: Path, database_url: str | None = None) -> None:
        self.settings = Settings(
            database_url=database_url
            or f"sqlite:///{tmp_path / 'conversation-service.db'}",
            data_dir=tmp_path / "data",
            upload_max_bytes=1024 * 1024,
            archive_max_bytes=4 * 1024 * 1024,
            max_attachment_total_bytes=4 * 1024 * 1024,
            jwt_signing_key="test-signing-key-at-least-32-bytes",
        )
        self.engine = make_engine(self.settings.database_url)
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        self.alice = AuthenticatedUser(
            id=_uuid(), username="alice", role=UserRole.ANALYST
        )
        self.bob = AuthenticatedUser(
            id=_uuid(), username="bob", role=UserRole.ANALYST
        )
        self.admin = AuthenticatedUser(
            id=_uuid(), username="admin", role=UserRole.ADMIN
        )
        with self.factory() as session:
            session.add_all(
                [
                    UserRow(
                        id=actor.id,
                        username=actor.username,
                        password_hash="unused",
                        role=actor.role.value,
                    )
                    for actor in (self.alice, self.bob, self.admin)
                ]
            )
            session.commit()

    def service(self, *, storage=None, event_writer=None, audit_writer=None):
        writer = self.factory()
        conversation_repository = ConversationRepository(writer)
        return ConversationService(
            conversation_repository=conversation_repository,
            task_repository=TaskRepository(writer),
            event_writer=event_writer
            or ConversationEventService(conversation_repository),
            audit_writer=audit_writer or AuditService(writer),
            storage=storage or ConversationStorageService(self.settings),
            settings=self.settings,
            session_factory=self.factory,
        )

    def close(self) -> None:
        self.engine.dispose()


@pytest.fixture
def service_env(tmp_path: Path):
    env = ServiceEnvironment(tmp_path)
    try:
        yield env
    finally:
        env.close()


def _counts(env: ServiceEnvironment, conversation_id: str) -> dict[str, int]:
    with env.factory() as session:
        message_ids = select(ConversationMessageRow.id).where(
            ConversationMessageRow.conversation_id == conversation_id
        )
        return {
            "messages": session.scalar(
                select(func.count()).select_from(ConversationMessageRow).where(
                    ConversationMessageRow.conversation_id == conversation_id
                )
            ),
            "attachments": session.scalar(
                select(func.count()).select_from(MessageAttachmentRow).where(
                    MessageAttachmentRow.message_id.in_(message_ids)
                )
            ),
            "tasks": session.scalar(select(func.count()).select_from(TaskRow)),
            "turns": session.scalar(
                select(func.count()).select_from(ConversationTurnRow).where(
                    ConversationTurnRow.conversation_id == conversation_id
                )
            ),
            "events": session.scalar(
                select(func.count()).select_from(ConversationEventRow).where(
                    ConversationEventRow.conversation_id == conversation_id
                )
            ),
            "jobs": session.scalar(select(func.count()).select_from(JobRunRow)),
        }


def test_constructor_rejects_miswired_writer_sessions_before_work(
    service_env: ServiceEnvironment,
) -> None:
    writer = service_env.factory()
    other = service_env.factory()
    repository = ConversationRepository(writer)
    with pytest.raises(ConversationRepositoryStateError, match="same writer Session"):
        ConversationService(
            conversation_repository=repository,
            task_repository=TaskRepository(other),
            event_writer=ConversationEventService(repository),
            audit_writer=AuditService(writer),
            storage=ConversationStorageService(service_env.settings),
            settings=service_env.settings,
            session_factory=service_env.factory,
        )
    writer.close()
    other.close()


def test_public_writes_require_a_clean_root_session(
    service_env: ServiceEnvironment,
) -> None:
    service = service_env.service()
    service.session.begin()
    with pytest.raises(ConversationRepositoryStateError, match="clean writer"):
        service.create(service_env.alice, ConversationCreate())
    service.session.rollback()
    service.session.add(
        ConversationRow(
            owner_id=service_env.alice.id,
            title="pending",
            settings_json="{}",
        )
    )
    with pytest.raises(ConversationRepositoryStateError, match="clean writer"):
        service.create(service_env.alice, ConversationCreate())
    service.session.rollback()
    service.session.close()


def test_crud_detail_rbac_ordering_archive_history_and_audits(
    service_env: ServiceEnvironment,
) -> None:
    service = service_env.service()
    first = service.create(service_env.alice, ConversationCreate(title="First"))
    second = service.create(service_env.bob, ConversationCreate(title="Second"))
    assert [item.id for item in service.list(service_env.alice)] == [first.id]
    assert {item.id for item in service.list(service_env.admin)} == {
        first.id,
        second.id,
    }
    with pytest.raises(ForbiddenResource):
        service.get_detail(service_env.bob, first.id)
    assert service.get_detail(service_env.alice, _uuid()) is None

    patched = service.patch(
        service_env.admin, first.id, ConversationPatch(title="Renamed")
    )
    archived = service.archive(service_env.alice, first.id)
    detail = service.get_detail(service_env.admin, first.id)
    assert patched.title == "Renamed"
    assert archived.status.value == "archived"
    assert detail is not None
    assert detail.conversation.status.value == "archived"
    assert detail.messages == [] and detail.turns == [] and detail.active_turn is None

    with service_env.factory() as session:
        actions = session.scalars(
            select(AuditEventRow.action).order_by(AuditEventRow.id)
        ).all()
        assert actions == [
            "conversation.create",
            "conversation.create",
            "conversation.patch",
            "conversation.archive",
        ]
        assert session.scalar(select(func.count()).select_from(ConversationRow)) == 2
    service.session.close()


def test_repository_idempotency_lookup_is_authorized_read_only(
    service_env: ServiceEnvironment,
) -> None:
    service = service_env.service()
    conversation = service.create(service_env.alice, ConversationCreate())
    sent = asyncio.run(
        service.send_message(
            service_env.alice,
            conversation.id,
            MessageSubmission(content="hello"),
            [],
            "lookup-key",
        )
    )
    with service_env.factory() as session:
        repository = ConversationRepository(session)
        found = repository.get_message_by_idempotency_key(
            service_env.alice, conversation.id, "lookup-key"
        )
        assert found == sent.message
        assert repository.get_message_by_idempotency_key(
            service_env.alice, conversation.id, "absent"
        ) is None
        with pytest.raises(ForbiddenResource):
            repository.get_message_by_idempotency_key(
                service_env.bob, conversation.id, "lookup-key"
            )
        assert not session.new and not session.dirty and not session.deleted
    service.session.close()


@pytest.mark.asyncio
async def test_one_text_message_composes_exact_rows_links_budgets_title_and_safe_audit(
    service_env: ServiceEnvironment,
) -> None:
    service = service_env.service()
    conversation = service.create(
        service_env.alice,
        ConversationCreate(
            settings=ConversationSettings(
                authorization_scope="owned target",
                safety_mode=SafetyMode.STANDARD,
            )
        ),
    )
    content = "  First\n\tmessage " + "界" * 50
    sent = await service.send_message(
        service_env.admin,
        conversation.id,
        MessageSubmission(content=content),
        [],
        "request-1",
    )
    assert sent.replayed is False
    assert sent.message.turn_id == sent.turn.id
    assert sent.turn.trigger_message_id == sent.message.id
    assert sent.turn.task_id is not None
    assert sent.attachments == []
    assert _counts(service_env, conversation.id) == {
        "messages": 1,
        "attachments": 0,
        "tasks": 1,
        "turns": 1,
        "events": 1,
        "jobs": 0,
    }
    detail = service.get_detail(service_env.alice, conversation.id)
    assert detail is not None
    assert detail.conversation.title == re.sub(r"\s+", " ", content).strip()[:40]
    assert detail.active_turn == sent.turn
    assert detail.messages[0].message == sent.message
    assert detail.turns == [sent.turn]

    with service_env.factory() as session:
        task = session.get(TaskRow, sent.turn.task_id)
        event = session.scalar(select(ConversationEventRow))
        audit = session.scalar(
            select(AuditEventRow).where(
                AuditEventRow.action == "conversation.message.send"
            )
        )
        assert task is not None and event is not None and audit is not None
        assert task.owner_id == service_env.alice.id
        assert task.goal == ("Conversation turn: " + re.sub(r"\s+", " ", content).strip())[:4000]
        assert task.authorization_scope == "owned target"
        assert task.safety_mode == "standard"
        assert (
            task.max_model_calls,
            task.max_input_tokens,
            task.max_output_tokens,
            task.max_steps,
        ) == (
            service_env.settings.max_model_calls_per_task,
            service_env.settings.max_input_tokens_per_task,
            service_env.settings.max_output_tokens_per_task,
            service_env.settings.max_steps_per_task,
        )
        assert sent.turn.budget.model_dump() == {
            "max_subtasks": 12,
            "max_model_calls_per_subtask": 4,
            "max_tool_calls_per_subtask": 6,
            "timeout_seconds": 180,
            "max_replans": 2,
            "max_context_tokens": 32_000,
        }
        event_payload = json.loads(event.payload_json)
        assert event.event_type == "conversation.message.created"
        assert set(event_payload) == {
            "conversation_id",
            "message_id",
            "turn_id",
            "task_id",
            "sequence",
            "plan_version",
            "attachment_count",
        }
        assert content not in event.payload_json
        assert audit.actor_id == service_env.admin.id
        assert audit.resource_type == "conversation" and audit.outcome == "success"
        assert content not in audit.details_json
    service.session.close()


@pytest.mark.asyncio
async def test_second_round_preserves_title_and_later_settings_snapshot_only(
    service_env: ServiceEnvironment,
) -> None:
    service = service_env.service()
    conversation = service.create(service_env.alice, ConversationCreate())
    first = await service.send_message(
        service_env.alice,
        conversation.id,
        MessageSubmission(content="first title"),
        [],
        "round-1",
    )
    service_env.settings.max_subtasks_per_turn = 7
    service_env.settings.max_model_calls_per_subtask = 2
    second = await service.send_message(
        service_env.alice,
        conversation.id,
        MessageSubmission(content="second title should not replace"),
        [],
        "round-2",
    )
    detail = service.get_detail(service_env.alice, conversation.id)
    assert detail is not None
    assert detail.conversation.title == "first title"
    assert [item.message.sequence for item in detail.messages] == [1, 2]
    assert [turn.plan_version for turn in detail.turns] == [1, 2]
    assert first.turn.budget.max_subtasks == 12
    assert first.turn.budget.max_model_calls_per_subtask == 4
    assert second.turn.budget.max_subtasks == 7
    assert second.turn.budget.max_model_calls_per_subtask == 2
    service.session.close()


@pytest.mark.asyncio
async def test_multiple_attachments_persist_exact_metadata_and_controlled_files(
    service_env: ServiceEnvironment,
) -> None:
    service = service_env.service()
    conversation = service.create(service_env.alice, ConversationCreate())
    sent = await service.send_message(
        service_env.alice,
        conversation.id,
        MessageSubmission(
            content="attachments",
            relative_paths=["folder/a.txt", "folder/b.bin"],
        ),
        [_upload("a.txt", b"alpha"), _upload("b.bin", b"beta")],
        "attachments-1",
    )
    assert len(sent.attachments) == 2
    assert {item.original_name for item in sent.attachments} == {"a.txt", "b.bin"}
    for item in sent.attachments:
        path = service.storage.data_dir / Path(*item.storage_ref.split("/"))
        assert path.is_file()
        assert item.message_id == sent.message.id
    service.session.close()


@pytest.mark.asyncio
async def test_sequential_replay_is_order_independent_and_multiplicity_sensitive(
    service_env: ServiceEnvironment,
) -> None:
    service = service_env.service()
    conversation = service.create(service_env.alice, ConversationCreate())
    submission = MessageSubmission(
        content="same",
        relative_paths=["one/a.txt", "two/b.txt", "one/a.txt"],
    )
    original = await service.send_message(
        service_env.alice,
        conversation.id,
        submission,
        [_upload("a.txt", b"a"), _upload("b.txt", b"b"), _upload("a.txt", b"a")],
        "same-key",
    )
    replay = await service.send_message(
        service_env.alice,
        conversation.id,
        MessageSubmission(
            content="same",
            relative_paths=["one/a.txt", "one/a.txt", "two/b.txt"],
        ),
        [_upload("a.txt", b"a"), _upload("a.txt", b"a"), _upload("b.txt", b"b")],
        "same-key",
    )
    assert replay.replayed is True
    assert replay.message == original.message
    assert replay.turn == original.turn
    assert Counter(
        (item.original_name, item.relative_path, item.content_type, item.size_bytes, item.sha256)
        for item in replay.attachments
    ) == Counter(
        (item.original_name, item.relative_path, item.content_type, item.size_bytes, item.sha256)
        for item in original.attachments
    )
    assert _counts(service_env, conversation.id) == {
        "messages": 1,
        "attachments": 3,
        "tasks": 1,
        "turns": 1,
        "events": 1,
        "jobs": 0,
    }
    with pytest.raises(AttachmentIdempotencyConflict):
        await service.send_message(
            service_env.alice,
            conversation.id,
            MessageSubmission(
                content="same", relative_paths=["one/a.txt", "two/b.txt"]
            ),
            [_upload("a.txt", b"a"), _upload("b.txt", b"b")],
            "same-key",
        )
    service.session.close()


@pytest.mark.asyncio
async def test_changed_text_conflicts_before_upload_and_archived_exact_replay_works(
    service_env: ServiceEnvironment,
) -> None:
    service = service_env.service()
    conversation = service.create(service_env.alice, ConversationCreate())
    original = await service.send_message(
        service_env.alice,
        conversation.id,
        MessageSubmission(content="same"),
        [],
        "same-key",
    )
    service.archive(service_env.alice, conversation.id)
    with pytest.raises(MessageIdempotencyConflict):
        await service.send_message(
            service_env.alice,
            conversation.id,
            MessageSubmission(content="different"),
            [_upload("unread.txt", b"must not read")],
            "same-key",
        )
    replay = await service.send_message(
        service_env.alice,
        conversation.id,
        MessageSubmission(content="same"),
        [],
        "same-key",
    )
    assert replay.replayed is True and replay.message == original.message
    with pytest.raises(ArchivedConversationError):
        await service.send_message(
            service_env.alice,
            conversation.id,
            MessageSubmission(content="new"),
            [],
            "new-key",
        )
    service.session.close()


class BarrierUpload(UploadFile):
    def __init__(self, barrier: Barrier, name: str, data: bytes) -> None:
        super().__init__(file=io.BytesIO(data), filename=name)
        self.barrier = barrier
        self.waited = False

    async def read(self, size: int = -1) -> bytes:
        if not self.waited:
            self.waited = True
            self.barrier.wait(timeout=10)
        return await super().read(size)


def test_concurrent_same_key_sqlite_has_one_composed_winner_and_no_loser_files(
    service_env: ServiceEnvironment,
) -> None:
    seed = service_env.service()
    conversation = seed.create(service_env.alice, ConversationCreate())
    seed.session.close()
    barrier = Barrier(2)

    def send(index: int):
        service = service_env.service()
        try:
            return asyncio.run(
                service.send_message(
                    service_env.alice,
                    conversation.id,
                    MessageSubmission(content="same", relative_paths=["a.txt"]),
                    [BarrierUpload(barrier, "a.txt", b"same bytes")],
                    "concurrent-key",
                )
            )
        finally:
            service.session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(send, range(2)))
    assert len({item.message.id for item in results}) == 1
    assert sorted(item.replayed for item in results) == [False, True]
    assert _counts(service_env, conversation.id) == {
        "messages": 1,
        "attachments": 1,
        "tasks": 1,
        "turns": 1,
        "events": 1,
        "jobs": 0,
    }
    message_root = (
        service_env.settings.data_dir
        / "conversations"
        / conversation.id
        / "messages"
    )
    assert len(list(message_root.iterdir())) == 1


class BlockingStageStorage(ConversationStorageService):
    def __init__(self, settings: Settings, staged: Event, release: Event) -> None:
        super().__init__(settings)
        self.staged = staged
        self.release = release

    async def stage_many(self, uploads, relative_paths):
        batch = await super().stage_many(uploads, relative_paths)
        self.staged.set()
        assert self.release.wait(timeout=10)
        return batch


def test_archive_winning_after_preflight_rejects_send_without_rows_or_files(
    service_env: ServiceEnvironment,
) -> None:
    seed = service_env.service()
    conversation = seed.create(service_env.alice, ConversationCreate())
    seed.session.close()
    staged = Event()
    release = Event()
    storage = BlockingStageStorage(service_env.settings, staged, release)

    def send():
        service = service_env.service(storage=storage)
        try:
            with pytest.raises(ArchivedConversationError):
                asyncio.run(
                    service.send_message(
                        service_env.alice,
                        conversation.id,
                        MessageSubmission(content="loser", relative_paths=[None]),
                        [_upload("a.txt", b"a")],
                        "archive-race",
                    )
                )
        finally:
            service.session.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(send)
        assert staged.wait(timeout=10)
        archiver = service_env.service()
        archiver.archive(service_env.alice, conversation.id)
        archiver.session.close()
        release.set()
        future.result(timeout=10)
    assert _counts(service_env, conversation.id)["messages"] == 0
    message_parent = (
        service_env.settings.data_dir
        / "conversations"
        / conversation.id
        / "messages"
    )
    assert not message_parent.exists() or not any(message_parent.iterdir())


class FailingEventWriter:
    def __init__(self, session) -> None:
        self.session = session

    def append_message_created(self, *args, **kwargs):
        raise RuntimeError("injected after-publish failure")


class FailingPublishStorage(ConversationStorageService):
    def publish(self, batch, conversation_id, message_id):
        raise RuntimeError("injected before-publish failure")


@pytest.mark.asyncio
async def test_failure_before_publish_rolls_back_message_and_cleans_staging(
    service_env: ServiceEnvironment,
) -> None:
    seed = service_env.service()
    conversation = seed.create(service_env.alice, ConversationCreate())
    seed.session.close()
    storage = FailingPublishStorage(service_env.settings)
    service = service_env.service(storage=storage)
    with pytest.raises(RuntimeError, match="before-publish"):
        await service.send_message(
            service_env.alice,
            conversation.id,
            MessageSubmission(content="failure", relative_paths=[None]),
            [_upload("a.txt", b"a")],
            "before-publish-failure",
        )
    assert _counts(service_env, conversation.id) == {
        "messages": 0,
        "attachments": 0,
        "tasks": 0,
        "turns": 0,
        "events": 0,
        "jobs": 0,
    }
    assert not storage.staging_root.exists() or not any(storage.staging_root.iterdir())
    service.session.close()


@pytest.mark.asyncio
async def test_failure_after_publish_rolls_back_all_rows_and_removes_workspace(
    service_env: ServiceEnvironment,
) -> None:
    seed = service_env.service()
    conversation = seed.create(service_env.alice, ConversationCreate())
    seed.session.close()
    writer = service_env.factory()
    repository = ConversationRepository(writer)
    service = ConversationService(
        conversation_repository=repository,
        task_repository=TaskRepository(writer),
        event_writer=FailingEventWriter(writer),
        audit_writer=AuditService(writer),
        storage=ConversationStorageService(service_env.settings),
        settings=service_env.settings,
        session_factory=service_env.factory,
    )
    with pytest.raises(RuntimeError, match="after-publish"):
        await service.send_message(
            service_env.alice,
            conversation.id,
            MessageSubmission(content="failure", relative_paths=[None]),
            [_upload("a.txt", b"a")],
            "failure-key",
        )
    assert _counts(service_env, conversation.id) == {
        "messages": 0,
        "attachments": 0,
        "tasks": 0,
        "turns": 0,
        "events": 0,
        "jobs": 0,
    }
    message_parent = (
        service_env.settings.data_dir
        / "conversations"
        / conversation.id
        / "messages"
    )
    assert not message_parent.exists() or not any(message_parent.iterdir())
    writer.close()


@pytest.mark.asyncio
async def test_explicit_commit_failure_compensates_but_uncertain_commit_keeps_files(
    service_env: ServiceEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = service_env.service()
    conversation = service.create(service_env.alice, ConversationCreate())
    original_commit = service.session.commit

    def fail_before_commit():
        raise RuntimeError("explicitly not committed")

    monkeypatch.setattr(service.session, "commit", fail_before_commit)
    with pytest.raises(RuntimeError, match="not committed"):
        await service.send_message(
            service_env.alice,
            conversation.id,
            MessageSubmission(content="fail", relative_paths=[None]),
            [_upload("a.txt", b"a")],
            "commit-fail",
        )
    assert _counts(service_env, conversation.id)["messages"] == 0
    message_parent = (
        service_env.settings.data_dir
        / "conversations"
        / conversation.id
        / "messages"
    )
    assert not message_parent.exists() or not any(message_parent.iterdir())

    monkeypatch.setattr(service.session, "commit", original_commit)
    uncertain_service = service_env.service()
    uncertain_original = uncertain_service.session.commit

    def commit_then_raise():
        uncertain_original()
        raise RuntimeError("connection lost after COMMIT")

    monkeypatch.setattr(uncertain_service.session, "commit", commit_then_raise)
    with pytest.raises(ConversationCommitOutcomeUnknown):
        await uncertain_service.send_message(
            service_env.alice,
            conversation.id,
            MessageSubmission(content="uncertain", relative_paths=[None]),
            [_upload("b.txt", b"b")],
            "commit-unknown",
        )
    assert _counts(service_env, conversation.id)["messages"] == 1
    message_root = service_env.settings.data_dir / "conversations" / conversation.id / "messages"
    assert len(list(message_root.iterdir())) == 1
    service.session.close()


@pytest.mark.asyncio
async def test_connection_invalidated_during_commit_is_treated_as_uncertain(
    service_env: ServiceEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = service_env.service()
    conversation = service.create(service_env.alice, ConversationCreate())

    def disconnect_during_commit():
        raise OperationalError(
            "COMMIT", {}, RuntimeError("connection lost"), connection_invalidated=True
        )

    monkeypatch.setattr(service.session, "commit", disconnect_during_commit)
    with pytest.raises(ConversationCommitOutcomeUnknown):
        await service.send_message(
            service_env.alice,
            conversation.id,
            MessageSubmission(content="uncertain", relative_paths=[None]),
            [_upload("uncertain.txt", b"keep for reconciliation")],
            "connection-invalidated",
        )
    message_parent = (
        service_env.settings.data_dir
        / "conversations"
        / conversation.id
        / "messages"
    )
    assert message_parent.exists() and len(list(message_parent.iterdir())) == 1


class FailingResponseService(ConversationService):
    def _message_send_read(self, *args, **kwargs):
        raise RuntimeError("injected response failure")


@pytest.mark.asyncio
async def test_response_failure_after_commit_keeps_files_for_idempotent_replay(
    service_env: ServiceEnvironment,
) -> None:
    seed = service_env.service()
    conversation = seed.create(service_env.alice, ConversationCreate())
    seed.session.close()
    writer = service_env.factory()
    repository = ConversationRepository(writer)
    service = FailingResponseService(
        conversation_repository=repository,
        task_repository=TaskRepository(writer),
        event_writer=ConversationEventService(repository),
        audit_writer=AuditService(writer),
        storage=ConversationStorageService(service_env.settings),
        settings=service_env.settings,
        session_factory=service_env.factory,
    )
    with pytest.raises(RuntimeError, match="response failure"):
        await service.send_message(
            service_env.alice,
            conversation.id,
            MessageSubmission(content="recover", relative_paths=[None]),
            [_upload("a.txt", b"a")],
            "response-fail",
        )
    writer.close()
    recovery = service_env.service()
    replay = await recovery.send_message(
        service_env.alice,
        conversation.id,
        MessageSubmission(content="recover", relative_paths=[None]),
        [_upload("a.txt", b"a")],
        "response-fail",
    )
    assert replay.replayed is True
    assert _counts(service_env, conversation.id)["messages"] == 1
    recovery.session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["", "x", "xy", "x" * 2001])
async def test_invalid_or_empty_scope_uses_exact_safe_fallback_and_short_goal_is_valid(
    service_env: ServiceEnvironment, scope: str
) -> None:
    service = service_env.service()
    conversation = service.create(
        service_env.alice,
        ConversationCreate(settings=ConversationSettings(authorization_scope=scope)),
    )
    sent = await service.send_message(
        service_env.alice,
        conversation.id,
        MessageSubmission(content="x"),
        [],
        f"scope-{len(scope)}",
    )
    with service_env.factory() as session:
        task = session.get(TaskRow, sent.turn.task_id)
        assert task is not None
        assert task.goal == "Conversation turn: x"
        assert task.authorization_scope == "No external target authorization granted."
    service.session.close()


@pytest.mark.asyncio
async def test_replay_without_turn_fails_closed(service_env: ServiceEnvironment) -> None:
    service = service_env.service()
    conversation = service.create(service_env.alice, ConversationCreate())
    sent = await service.send_message(
        service_env.alice,
        conversation.id,
        MessageSubmission(content="corrupt"),
        [],
        "corrupt-key",
    )
    with service_env.factory() as session:
        row = session.get(ConversationMessageRow, sent.message.id)
        assert row is not None
        row.turn_id = None
        session.commit()
    with pytest.raises(ConversationReplayCorruptState):
        await service.send_message(
            service_env.alice,
            conversation.id,
            MessageSubmission(content="corrupt"),
            [],
            "corrupt-key",
        )
    service.session.close()


@pytest.mark.asyncio
async def test_service_key_owned_by_non_user_message_conflicts_closed(
    service_env: ServiceEnvironment,
) -> None:
    service = service_env.service()
    conversation = service.create(service_env.alice, ConversationCreate())
    with service_env.factory() as session:
        repository = ConversationRepository(session)
        message = repository.add_message(
            service_env.alice,
            conversation.id,
            ConversationMessageWrite(
                role="assistant",
                kind="assistant_status",
                content="same text",
                idempotency_key="foreign-key",
            ),
        ).message
        repository.create_turn(
            service_env.alice,
            conversation.id,
            ConversationTurnCreate(
                trigger_message_id=message.id,
                budget=TurnBudgetSnapshot(
                    max_subtasks=1,
                    max_model_calls_per_subtask=1,
                    max_tool_calls_per_subtask=1,
                    timeout_seconds=1,
                    max_replans=1,
                    max_context_tokens=1,
                ),
            ),
        )
    with pytest.raises(MessageIdempotencyConflict):
        await service.send_message(
            service_env.alice,
            conversation.id,
            MessageSubmission(content="same text"),
            [],
            "foreign-key",
        )
    service.session.close()


def test_concurrent_same_key_postgresql_read_committed_barrier(tmp_path: Path) -> None:
    database_url = os.environ.get("SECAGENT_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("SECAGENT_TEST_POSTGRES_URL is not configured")
    cleanup_engine = make_engine(database_url)
    Base.metadata.drop_all(cleanup_engine)
    cleanup_engine.dispose()
    env = ServiceEnvironment(tmp_path, database_url=database_url)
    try:
        with env.factory() as session:
            assert session.execute(select(func.current_setting("transaction_isolation"))).scalar_one() == "read committed"
        seed = env.service()
        conversation = seed.create(env.alice, ConversationCreate())
        seed.session.close()
        barrier = Barrier(2)

        def send(index: int):
            service = env.service()
            try:
                if index == 0:
                    paths = ["one/a.txt", "two/b.txt", "one/a.txt"]
                    uploads = [
                        BarrierUpload(barrier, "a.txt", b"a"),
                        _upload("b.txt", b"b"),
                        _upload("a.txt", b"a"),
                    ]
                else:
                    paths = ["one/a.txt", "one/a.txt", "two/b.txt"]
                    uploads = [
                        BarrierUpload(barrier, "a.txt", b"a"),
                        _upload("a.txt", b"a"),
                        _upload("b.txt", b"b"),
                    ]
                return asyncio.run(
                    service.send_message(
                        env.alice,
                        conversation.id,
                        MessageSubmission(content="postgres race", relative_paths=paths),
                        uploads,
                        "postgres-concurrent-key",
                    )
                )
            finally:
                service.session.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(send, range(2)))
        assert len({item.message.id for item in results}) == 1
        assert sorted(item.replayed for item in results) == [False, True]
        assert _counts(env, conversation.id) == {
            "messages": 1,
            "attachments": 3,
            "tasks": 1,
            "turns": 1,
            "events": 1,
            "jobs": 0,
        }
        with env.factory() as session:
            row = session.get(ConversationRow, conversation.id)
            assert row is not None
            assert row.next_message_sequence == 2
            assert row.next_turn_plan_version == 2
    finally:
        Base.metadata.drop_all(env.engine)
        env.close()
