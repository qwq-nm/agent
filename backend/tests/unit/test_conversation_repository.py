from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from secagent.api.errors import ForbiddenResource
from secagent.auth.dependencies import AuthenticatedUser
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
    ConversationTurnCreate,
    ConversationTurnRead,
    TurnBudgetSnapshot,
)
from secagent.conversation_repository import (
    ConversationInvariantError,
    ConversationRepository,
    ConversationRepositoryStateError,
    MessageIdempotencyConflict,
)
from secagent.db import Base, make_engine
from secagent.db_models import (
    ConversationEventRow,
    ConversationMessageRow,
    ConversationRow,
    ConversationTurnRow,
    MessageAttachmentRow,
    TaskRow,
    UserRow,
)
from secagent.domain import UserRole


def _actor(name: str, role: UserRole = UserRole.ANALYST) -> AuthenticatedUser:
    return AuthenticatedUser(id=str(uuid4()), username=name, role=role)


@pytest.fixture
def repository_env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'conversation-repository.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    alice = _actor("alice")
    bob = _actor("bob")
    admin = _actor("admin", UserRole.ADMIN)
    with factory() as session:
        session.add_all(
            [
                UserRow(
                    id=actor.id,
                    username=actor.username,
                    password_hash="unused",
                    role=actor.role.value,
                )
                for actor in (alice, bob, admin)
            ]
        )
        session.commit()
    try:
        yield factory, alice, bob, admin
    finally:
        engine.dispose()


def _message(content: str, *, key: str | None = None, turn_id: str | None = None):
    return ConversationMessageWrite(
        role="user",
        kind="user_text",
        content=content,
        idempotency_key=key,
        turn_id=turn_id,
    )


def _budget() -> TurnBudgetSnapshot:
    return TurnBudgetSnapshot(
        max_subtasks=3,
        max_model_calls_per_subtask=2,
        max_tool_calls_per_subtask=4,
        timeout_seconds=60,
        max_replans=1,
        max_context_tokens=8_000,
    )


def _attachment(name: str = "evidence.txt") -> AttachmentMetadataCreate:
    return AttachmentMetadataCreate(
        original_name=name,
        storage_ref=f"objects/{name}",
        relative_path=f"uploads/{name}",
        content_type="text/plain",
        size_bytes=7,
        sha256="a" * 64,
    )


def _create_conversation(
    factory: sessionmaker[Session], actor: AuthenticatedUser, title: str = "Case"
) -> ConversationRead:
    with factory() as session:
        return ConversationRepository(session).create_conversation(
            actor,
            ConversationCreate(
                title=title,
                settings=ConversationSettings(
                    authorization_scope="approved logs",
                    allowed_targets=["logs.example.test"],
                    requested_parallelism=2,
                ),
            ),
        )


def test_structured_conversation_crud_owner_and_deterministic_list(repository_env):
    factory, alice, _bob, admin = repository_env
    with factory() as session:
        repository = ConversationRepository(session)
        first = repository.create_conversation(
            alice, ConversationCreate(title="First"), commit=False
        )
        second = repository.create_conversation(
            alice, ConversationCreate(title="Second"), commit=False
        )
        repository.commit()

        fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session.execute(
            text("UPDATE conversations SET created_at = :created_at"),
            {"created_at": fixed},
        )
        session.commit()

        patched = repository.patch_conversation(
            alice,
            first.id,
            ConversationPatch(
                title="Renamed",
                settings=ConversationSettings(authorization_scope="new scope"),
            ),
        )
        archived = repository.archive_conversation(alice, first.id)

        assert isinstance(first, ConversationRead)
        assert first.owner_id == alice.id
        assert patched.title == "Renamed"
        assert patched.settings.authorization_scope == "new scope"
        assert archived.status.value == "archived"
        reread = repository.get_conversation(alice, first.id)
        assert reread is not None
        assert reread.id == archived.id
        assert reread.status == archived.status
        assert reread.settings == archived.settings
        expected = sorted([first.id, second.id], reverse=True)
        assert [item.id for item in repository.list_conversations(alice)] == expected
        assert {item.id for item in repository.list_conversations(admin)} == set(
            expected
        )
        assert repository.get_conversation(alice, str(uuid4())) is None


@pytest.mark.parametrize(
    ("table", "column", "bad_value"),
    [
        ("conversations", "settings_json", '{"unknown":true}'),
        ("conversation_turns", "budget_json", '{"max_subtasks":-1}'),
        ("conversation_events", "payload_json", "NaN"),
    ],
)
def test_corrupt_structured_json_fails_closed(
    repository_env, table: str, column: str, bad_value: str
):
    factory, alice, _bob, _admin = repository_env
    conversation = _create_conversation(factory, alice)
    with factory() as session:
        repository = ConversationRepository(session)
        message = repository.add_message(
            alice, conversation.id, _message("trigger")
        ).message
        turn = repository.create_turn(
            alice,
            conversation.id,
            ConversationTurnCreate(trigger_message_id=message.id, budget=_budget()),
        )
        event_read = repository.append_event(
            alice,
            conversation.id,
            ConversationEventCreate(event_type="turn.created", payload={"ok": True}),
        )
        row_id = {
            "conversations": conversation.id,
            "conversation_turns": turn.id,
            "conversation_events": event_read.cursor,
        }[table]
        session.execute(
            text(f"UPDATE {table} SET {column} = :bad WHERE id = :row_id"),
            {"bad": bad_value, "row_id": row_id},
        )
        session.commit()
        session.expire_all()

        with pytest.raises((ValidationError, ValueError)):
            if table == "conversations":
                repository.get_conversation(alice, conversation.id)
            elif table == "conversation_turns":
                repository.get_turn(alice, conversation.id, turn.id)
            else:
                repository.events_after(alice, conversation.id)


def test_analyst_isolation_for_all_child_families_and_admin_access(repository_env):
    factory, alice, bob, admin = repository_env
    conversation = _create_conversation(factory, alice)
    with factory() as session:
        repository = ConversationRepository(session)
        message = repository.add_message(
            alice, conversation.id, _message("secret")
        ).message
        attachment = repository.add_attachment(
            alice, conversation.id, message.id, _attachment()
        )
        turn = repository.create_turn(
            alice,
            conversation.id,
            ConversationTurnCreate(trigger_message_id=message.id, budget=_budget()),
        )
        event_read = repository.append_event(
            alice,
            conversation.id,
            ConversationEventCreate(
                event_type="turn.created", payload={"secret": True}, turn_id=turn.id
            ),
        )

        denied = [
            lambda: repository.get_conversation(bob, conversation.id),
            lambda: repository.patch_conversation(
                bob, conversation.id, ConversationPatch(title="stolen")
            ),
            lambda: repository.archive_conversation(bob, conversation.id),
            lambda: repository.get_message(bob, conversation.id, message.id),
            lambda: repository.list_messages(bob, conversation.id),
            lambda: repository.add_message(bob, conversation.id, _message("bad")),
            lambda: repository.list_attachments(bob, conversation.id, message.id),
            lambda: repository.add_attachment(
                bob, conversation.id, message.id, _attachment("bad.txt")
            ),
            lambda: repository.get_turn(bob, conversation.id, turn.id),
            lambda: repository.list_turns(bob, conversation.id),
            lambda: repository.create_turn(
                bob,
                conversation.id,
                ConversationTurnCreate(
                    trigger_message_id=message.id, budget=_budget()
                ),
            ),
            lambda: repository.set_active_turn(bob, conversation.id, turn.id),
            lambda: repository.clear_active_turn(
                bob, conversation.id, expected_turn_id=turn.id
            ),
            lambda: repository.append_event(
                bob,
                conversation.id,
                ConversationEventCreate(event_type="stolen"),
            ),
            lambda: repository.events_after(bob, conversation.id),
        ]
        for operation in denied:
            with pytest.raises(ForbiddenResource) as caught:
                operation()
            assert caught.value.message == "Access to conversation is forbidden"

        admin_message = repository.get_message(admin, conversation.id, message.id)
        assert admin_message is not None
        assert admin_message.id == message.id
        assert [item.id for item in repository.list_attachments(
            admin, conversation.id, message.id
        )] == [attachment.id]
        admin_turn = repository.get_turn(admin, conversation.id, turn.id)
        assert admin_turn is not None
        assert admin_turn.id == turn.id
        assert [item.cursor for item in repository.events_after(
            admin, conversation.id
        )] == [event_read.cursor]


def test_cross_conversation_and_task_owner_invariants_leave_no_mutations(
    repository_env,
):
    factory, alice, bob, _admin = repository_env
    first = _create_conversation(factory, alice, "First")
    second = _create_conversation(factory, alice, "Second")
    with factory() as session:
        repository = ConversationRepository(session)
        first_trigger = repository.add_message(
            alice, first.id, _message("first trigger")
        ).message
        second_trigger = repository.add_message(
            alice, second.id, _message("second trigger")
        ).message
        second_turn = repository.create_turn(
            alice,
            second.id,
            ConversationTurnCreate(
                trigger_message_id=second_trigger.id, budget=_budget()
            ),
        )
        foreign_task = TaskRow(
            owner_id=bob.id,
            goal="foreign",
            authorization_scope="foreign",
            route_mode="auto",
        )
        session.add(foreign_task)
        session.commit()

        before = session.execute(
            select(
                ConversationRow.next_message_sequence,
                ConversationRow.next_turn_plan_version,
            ).where(ConversationRow.id == first.id)
        ).one()
        failures = [
            lambda: repository.add_message(
                alice, first.id, _message("bad turn", turn_id=second_turn.id)
            ),
            lambda: repository.add_attachment(
                alice, first.id, second_trigger.id, _attachment()
            ),
            lambda: repository.create_turn(
                alice,
                first.id,
                ConversationTurnCreate(
                    trigger_message_id=second_trigger.id, budget=_budget()
                ),
            ),
            lambda: repository.create_turn(
                alice,
                first.id,
                ConversationTurnCreate(
                    trigger_message_id=first_trigger.id,
                    replan_from_turn_id=second_turn.id,
                    budget=_budget(),
                ),
            ),
            lambda: repository.create_turn(
                alice,
                first.id,
                ConversationTurnCreate(
                    trigger_message_id=first_trigger.id,
                    task_id=foreign_task.id,
                    budget=_budget(),
                ),
            ),
            lambda: repository.append_event(
                alice,
                first.id,
                ConversationEventCreate(event_type="bad", turn_id=second_turn.id),
            ),
            lambda: repository.set_active_turn(alice, first.id, second_turn.id),
        ]
        for operation in failures:
            with pytest.raises(ConversationInvariantError):
                operation()

        session.expire_all()
        after = session.execute(
            select(
                ConversationRow.next_message_sequence,
                ConversationRow.next_turn_plan_version,
            ).where(ConversationRow.id == first.id)
        ).one()
        assert after == before
        assert session.scalar(
            select(func.count())
            .select_from(MessageAttachmentRow)
            .join(ConversationMessageRow)
            .where(ConversationMessageRow.conversation_id == first.id)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(ConversationTurnRow).where(
                ConversationTurnRow.conversation_id == first.id
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(ConversationEventRow).where(
                ConversationEventRow.conversation_id == first.id
            )
        ) == 0


def test_allocator_first_statement_is_conditional_conversation_update(repository_env):
    factory, alice, _bob, _admin = repository_env
    conversation = _create_conversation(factory, alice)
    statements: list[str] = []
    engine = factory.kw["bind"]

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.split()).upper())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with factory() as session:
            ConversationRepository(session).add_message(
                alice, conversation.id, _message("lock first")
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    first_dml = next(
        statement
        for statement in statements
        if statement.startswith(("SELECT", "UPDATE", "INSERT", "DELETE"))
    )
    assert first_dml.startswith("UPDATE CONVERSATIONS")
    assert "RETURNING" in first_dml
    assert "OWNER_ID" in first_dml


@pytest.mark.parametrize("allocator", ["message", "turn"])
@pytest.mark.parametrize("state_kind", ["new", "dirty", "deleted"])
def test_allocator_rejects_pending_caller_state_before_any_sql(
    repository_env, allocator: str, state_kind: str
):
    factory, alice, bob, _admin = repository_env
    conversation = _create_conversation(factory, alice)
    with factory() as seed_session:
        trigger_id = ConversationRepository(seed_session).add_message(
            alice, conversation.id, _message("trigger")
        ).message.id

    with factory() as session:
        if state_kind == "new":
            pending = UserRow(
                id=str(uuid4()),
                username=f"pending-{uuid4()}",
                password_hash="unused",
                role=UserRole.ANALYST.value,
            )
            session.add(pending)
        else:
            pending = session.get(UserRow, bob.id)
            assert pending is not None
            if state_kind == "dirty":
                pending.username = "bob-dirty"
            else:
                session.delete(pending)

        statements: list[str] = []
        engine = factory.kw["bind"]

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(" ".join(statement.split()).upper())

        event.listen(engine, "before_cursor_execute", capture)
        try:
            repository = ConversationRepository(session)
            with pytest.raises(ConversationRepositoryStateError):
                if allocator == "message":
                    repository.add_message(
                        alice, conversation.id, _message("must not flush")
                    )
                else:
                    repository.create_turn(
                        alice,
                        conversation.id,
                        ConversationTurnCreate(
                            trigger_message_id=trigger_id, budget=_budget()
                        ),
                    )
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        assert statements == []
        collection = {
            "new": session.new,
            "dirty": session.dirty,
            "deleted": session.deleted,
        }[state_kind]
        assert pending in collection
        session.rollback()


def test_commit_failure_rolls_back_full_write_and_session_remains_usable(
    repository_env, monkeypatch
):
    factory, alice, _bob, _admin = repository_env
    conversation = _create_conversation(factory, alice)
    with factory() as session:
        repository = ConversationRepository(session)
        original_commit = session.commit
        original_rollback = session.rollback
        rollback_calls = 0

        def fail_commit() -> None:
            raise RuntimeError("injected commit failure")

        def track_rollback() -> None:
            nonlocal rollback_calls
            rollback_calls += 1
            original_rollback()

        monkeypatch.setattr(session, "commit", fail_commit)
        monkeypatch.setattr(session, "rollback", track_rollback)
        with pytest.raises(RuntimeError, match="injected commit failure"):
            repository.add_message(
                alice, conversation.id, _message("must roll back")
            )

        assert rollback_calls == 1
        assert not session.in_transaction()
        monkeypatch.setattr(session, "commit", original_commit)
        created = repository.add_message(
            alice, conversation.id, _message("usable afterward")
        ).message
        assert created.sequence == 1

    with factory() as session:
        row = session.get(ConversationRow, conversation.id)
        assert row is not None
        assert row.next_message_sequence == 2
        assert session.scalar(
            select(func.count()).select_from(ConversationMessageRow).where(
                ConversationMessageRow.conversation_id == conversation.id
            )
        ) == 1


def test_parallel_message_allocation_is_unique_contiguous_and_exact(repository_env):
    factory, alice, _bob, _admin = repository_env
    conversation = _create_conversation(factory, alice)
    workers = 12
    barrier = Barrier(workers)

    def append(index: int) -> tuple[int, bool]:
        with factory() as session:
            barrier.wait()
            result = ConversationRepository(session).add_message(
                alice, conversation.id, _message(f"message {index}")
            )
            return result.message.sequence, result.created

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(append, range(workers)))

    assert sorted(sequence for sequence, _created in results) == list(
        range(1, workers + 1)
    )
    assert all(created for _sequence, created in results)
    with factory() as session:
        row = session.get(ConversationRow, conversation.id)
        assert row is not None
        assert row.next_message_sequence == workers + 1
        assert session.scalar(
            select(func.count()).select_from(ConversationMessageRow)
        ) == workers


def test_parallel_same_key_is_one_row_no_gap_and_conflicts_on_identity(repository_env):
    factory, alice, _bob, _admin = repository_env
    conversation = _create_conversation(factory, alice)
    barrier = Barrier(2)

    def append() -> tuple[str, int, bool]:
        with factory() as session:
            barrier.wait()
            result = ConversationRepository(session).add_message(
                alice, conversation.id, _message("same", key="request-1")
            )
            return result.message.id, result.message.sequence, result.created

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: append(), range(2)))

    assert len({item[0] for item in results}) == 1
    assert {item[1] for item in results} == {1}
    assert sorted(item[2] for item in results) == [False, True]
    with factory() as session:
        repository = ConversationRepository(session)
        replay = repository.add_message(
            alice, conversation.id, _message("same", key="request-1")
        )
        assert replay.created is False
        with pytest.raises(MessageIdempotencyConflict):
            repository.add_message(
                alice, conversation.id, _message("different", key="request-1")
            )
        row = session.get(ConversationRow, conversation.id)
        assert row is not None
        assert row.next_message_sequence == 2
        assert session.scalar(
            select(func.count()).select_from(ConversationMessageRow)
        ) == 1


def test_parallel_turns_are_contiguous_highest_active_and_same_trigger_is_unique(
    repository_env,
):
    factory, alice, _bob, _admin = repository_env
    conversation = _create_conversation(factory, alice)
    with factory() as session:
        repository = ConversationRepository(session)
        trigger_ids = [
            repository.add_message(alice, conversation.id, _message(f"t{index}"))
            .message.id
            for index in range(10)
        ]
        shared_trigger = repository.add_message(
            alice, conversation.id, _message("shared")
        ).message.id

    barrier = Barrier(len(trigger_ids))

    def create(trigger_id: str) -> tuple[str, int]:
        with factory() as session:
            barrier.wait()
            turn = ConversationRepository(session).create_turn(
                alice,
                conversation.id,
                ConversationTurnCreate(trigger_message_id=trigger_id, budget=_budget()),
            )
            return turn.id, turn.plan_version

    with ThreadPoolExecutor(max_workers=len(trigger_ids)) as executor:
        turns = list(executor.map(create, trigger_ids))

    assert sorted(version for _turn_id, version in turns) == list(range(1, 11))
    with factory() as session:
        row = session.get(ConversationRow, conversation.id)
        assert row is not None
        highest = max(turns, key=lambda item: item[1])
        assert row.active_turn_id == highest[0]
        assert row.next_turn_plan_version == 11

    same_barrier = Barrier(2)

    def create_same() -> str:
        with factory() as session:
            same_barrier.wait()
            try:
                ConversationRepository(session).create_turn(
                    alice,
                    conversation.id,
                    ConversationTurnCreate(
                        trigger_message_id=shared_trigger, budget=_budget()
                    ),
                )
            except ConversationInvariantError:
                return "conflict"
            return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: create_same(), range(2)))
    assert sorted(outcomes) == ["conflict", "created"]
    with factory() as session:
        row = session.get(ConversationRow, conversation.id)
        assert row is not None
        assert row.next_turn_plan_version == 12
        assert session.scalar(
            select(func.count()).select_from(ConversationTurnRow).where(
                ConversationTurnRow.conversation_id == conversation.id
            )
        ) == 11


def test_stale_cached_trigger_cannot_create_second_turn_or_counter_gap(
    repository_env,
):
    factory, alice, _bob, _admin = repository_env
    conversation = _create_conversation(factory, alice)
    with factory() as seed_session:
        trigger = ConversationRepository(seed_session).add_message(
            alice, conversation.id, _message("cached trigger")
        ).message

    with factory() as stale_session:
        stale_row = stale_session.get(ConversationMessageRow, trigger.id)
        assert stale_row is not None
        assert stale_row.turn_id is None

        with factory() as winning_session:
            first_turn = ConversationRepository(winning_session).create_turn(
                alice,
                conversation.id,
                ConversationTurnCreate(trigger_message_id=trigger.id, budget=_budget()),
            )

        assert stale_row.turn_id is None
        with pytest.raises(ConversationInvariantError):
            ConversationRepository(stale_session).create_turn(
                alice,
                conversation.id,
                ConversationTurnCreate(trigger_message_id=trigger.id, budget=_budget()),
            )

    with factory() as session:
        linked = session.get(ConversationMessageRow, trigger.id)
        conversation_row = session.get(ConversationRow, conversation.id)
        assert linked is not None
        assert conversation_row is not None
        assert linked.turn_id == first_turn.id
        assert conversation_row.active_turn_id == first_turn.id
        assert conversation_row.next_turn_plan_version == 2
        assert session.scalar(
            select(func.count()).select_from(ConversationTurnRow).where(
                ConversationTurnRow.conversation_id == conversation.id
            )
        ) == 1


def test_active_turn_highest_only_and_expected_clear_is_cas(repository_env):
    factory, alice, _bob, _admin = repository_env
    conversation = _create_conversation(factory, alice)
    with factory() as session:
        repository = ConversationRepository(session)
        first_message = repository.add_message(
            alice, conversation.id, _message("first")
        ).message
        first_turn = repository.create_turn(
            alice,
            conversation.id,
            ConversationTurnCreate(trigger_message_id=first_message.id, budget=_budget()),
        )
        second_message = repository.add_message(
            alice, conversation.id, _message("second")
        ).message
        second_turn = repository.create_turn(
            alice,
            conversation.id,
            ConversationTurnCreate(
                trigger_message_id=second_message.id, budget=_budget()
            ),
        )

        with pytest.raises(ConversationInvariantError):
            repository.set_active_turn(alice, conversation.id, first_turn.id)
        stale = repository.clear_active_turn(
            alice, conversation.id, expected_turn_id=first_turn.id
        )
        assert stale.active_turn_id == second_turn.id
        cleared = repository.clear_active_turn(
            alice, conversation.id, expected_turn_id=second_turn.id
        )
        assert cleared.active_turn_id is None
        restored = repository.set_active_turn(alice, conversation.id, second_turn.id)
        assert restored.active_turn_id == second_turn.id


def test_commit_false_composes_and_caller_rollback_removes_rows_and_counters(
    repository_env,
):
    factory, alice, _bob, _admin = repository_env
    conversation = _create_conversation(factory, alice)
    other = _create_conversation(factory, alice, "Other")
    with factory() as seed_session:
        other_message = ConversationRepository(seed_session).add_message(
            alice, other.id, _message("other")
        ).message
        other_turn = ConversationRepository(seed_session).create_turn(
            alice,
            other.id,
            ConversationTurnCreate(trigger_message_id=other_message.id, budget=_budget()),
        )

    with factory() as session:
        repository = ConversationRepository(session)
        message = repository.add_message(
            alice, conversation.id, _message("atomic"), commit=False
        ).message
        attachments = [
            repository.add_attachment(
                alice,
                conversation.id,
                message.id,
                _attachment(f"{index}.txt"),
                commit=False,
            )
            for index in range(2)
        ]
        turn = repository.create_turn(
            alice,
            conversation.id,
            ConversationTurnCreate(trigger_message_id=message.id, budget=_budget()),
            commit=False,
        )
        event_read = repository.append_event(
            alice,
            conversation.id,
            ConversationEventCreate(event_type="turn.created", turn_id=turn.id),
            commit=False,
        )
        assert isinstance(message, ConversationMessageRead)
        assert all(isinstance(item, AttachmentRead) for item in attachments)
        assert isinstance(turn, ConversationTurnRead)
        assert isinstance(event_read, ConversationEventRead)

        with pytest.raises(ConversationInvariantError):
            repository.append_event(
                alice,
                conversation.id,
                ConversationEventCreate(event_type="late.failure", turn_id=other_turn.id),
                commit=False,
            )
        pending = session.get(
            ConversationRow, conversation.id, populate_existing=True
        )
        assert pending is not None
        assert pending.next_message_sequence == 2
        assert pending.next_turn_plan_version == 2
        assert pending.active_turn_id == turn.id
        assert session.scalar(
            select(func.count()).select_from(ConversationMessageRow).where(
                ConversationMessageRow.conversation_id == conversation.id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(MessageAttachmentRow).where(
                MessageAttachmentRow.message_id == message.id
            )
        ) == 2
        assert session.scalar(
            select(func.count()).select_from(ConversationEventRow).where(
                ConversationEventRow.conversation_id == conversation.id
            )
        ) == 1
        repository.rollback()

    with factory() as session:
        row = session.get(ConversationRow, conversation.id)
        assert row is not None
        assert row.next_message_sequence == 1
        assert row.next_turn_plan_version == 1
        assert row.active_turn_id is None
        assert session.scalar(
            select(func.count()).select_from(ConversationMessageRow).where(
                ConversationMessageRow.conversation_id == conversation.id
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(ConversationTurnRow).where(
                ConversationTurnRow.conversation_id == conversation.id
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(ConversationEventRow).where(
                ConversationEventRow.conversation_id == conversation.id
            )
        ) == 0


def test_events_use_global_cursor_strict_boundary_order_and_isolation(repository_env):
    factory, alice, _bob, _admin = repository_env
    first = _create_conversation(factory, alice, "First")
    second = _create_conversation(factory, alice, "Second")
    with factory() as session:
        repository = ConversationRepository(session)
        events = [
            repository.append_event(
                alice,
                conversation_id,
                ConversationEventCreate(
                    event_type=f"event.{index}",
                    payload={"index": index},
                    subtask_id=f"opaque-{index}",
                ),
            )
            for index, conversation_id in enumerate(
                [first.id, second.id, first.id, second.id, first.id], start=1
            )
        ]
        assert [item.cursor for item in events] == sorted(
            {item.cursor for item in events}
        )
        assert [item.payload["index"] for item in repository.events_after(
            alice, first.id
        )] == [1, 3, 5]
        assert [item.payload["index"] for item in repository.events_after(
            alice, first.id, cursor=events[0].cursor
        )] == [3, 5]
        assert repository.events_after(
            alice, first.id, cursor=events[-1].cursor
        ) == []


def test_event_append_locks_owned_conversation_before_insert(repository_env):
    factory, alice, _bob, _admin = repository_env
    conversation = _create_conversation(factory, alice)
    statements: list[str] = []
    engine = factory.kw["bind"]

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.split()).upper()
        if normalized.startswith(("SELECT", "UPDATE", "INSERT", "DELETE")):
            statements.append(normalized)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with factory() as session:
            ConversationRepository(session).append_event(
                alice,
                conversation.id,
                ConversationEventCreate(event_type="serialized"),
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    event_insert = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("INSERT INTO CONVERSATION_EVENTS")
    )
    assert event_insert > 0
    assert statements[0].startswith("UPDATE CONVERSATIONS")
    assert "RETURNING" in statements[0]
    assert "OWNER_ID" in statements[0]


def test_concurrent_same_conversation_events_have_exact_visible_cursor_order(
    repository_env,
):
    factory, alice, _bob, _admin = repository_env
    conversation = _create_conversation(factory, alice)
    workers = 8
    barrier = Barrier(workers)

    def append(index: int) -> tuple[int, int]:
        with factory() as session:
            barrier.wait()
            created = ConversationRepository(session).append_event(
                alice,
                conversation.id,
                ConversationEventCreate(
                    event_type="concurrent", payload={"index": index}
                ),
            )
            return created.cursor, index

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(append, range(workers)))

    with factory() as session:
        visible = ConversationRepository(session).events_after(
            alice, conversation.id
        )
    assert [item.cursor for item in visible] == sorted(cursor for cursor, _ in results)
    assert {item.payload["index"] for item in visible} == set(range(workers))


def test_message_turn_attachment_pagination_and_validation(repository_env):
    factory, alice, _bob, _admin = repository_env
    conversation = _create_conversation(factory, alice)
    with factory() as session:
        repository = ConversationRepository(session)
        messages = [
            repository.add_message(alice, conversation.id, _message(str(index)))
            .message
            for index in range(5)
        ]
        turns = [
            repository.create_turn(
                alice,
                conversation.id,
                ConversationTurnCreate(
                    trigger_message_id=message.id, budget=_budget()
                ),
            )
            for message in messages[:3]
        ]
        attachments = [
            repository.add_attachment(
                alice, conversation.id, messages[4].id, _attachment(f"{index}.txt")
            )
            for index in range(3)
        ]
        fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        session.execute(
            text(
                "UPDATE message_attachments SET created_at = :created_at "
                "WHERE message_id = :message_id"
            ),
            {"created_at": fixed, "message_id": messages[4].id},
        )
        session.commit()

        assert [item.id for item in repository.list_messages(
            alice, conversation.id, after_sequence=2, limit=2
        )] == [item.id for item in messages[2:4]]
        assert [item.id for item in repository.list_turns(
            alice, conversation.id, after_plan_version=1, limit=1
        )] == [turns[1].id]
        assert [item.id for item in repository.list_attachments(
            alice, conversation.id, messages[4].id
        )] == sorted(item.id for item in attachments)

        invalid_calls = [
            lambda: repository.list_conversations(alice, limit=201),
            lambda: repository.list_messages(alice, conversation.id, limit=501),
            lambda: repository.list_messages(
                alice, conversation.id, after_sequence=-1
            ),
            lambda: repository.list_turns(alice, conversation.id, limit=501),
            lambda: repository.list_turns(
                alice, conversation.id, after_plan_version=True
            ),
            lambda: repository.list_attachments(
                alice, conversation.id, messages[4].id, limit=501
            ),
            lambda: repository.events_after(alice, conversation.id, limit=1_001),
            lambda: repository.events_after(alice, conversation.id, cursor=True),
        ]
        for call in invalid_calls:
            with pytest.raises(ValueError):
                call()
