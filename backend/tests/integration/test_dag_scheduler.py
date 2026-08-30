from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from secagent.auth.dependencies import AuthenticatedUser
from secagent.conversation_decomposition import (
    DecompositionDocument,
    RouteReasonCode,
)
from secagent.conversation_domain import (
    ConversationCreate,
    ConversationMessageWrite,
    ConversationTurnCreate,
    TurnBudgetSnapshot,
)
from secagent.conversation_repository import ConversationRepository
from secagent.dag_domain import (
    JobKind,
    SubtaskStatus,
    SubtaskTransitionError,
)
from secagent.db import Base, make_engine
from secagent.db_models import (
    ConversationTurnRow,
    JobRunRow,
    SubtaskRow,
    UserRow,
)
from secagent.domain import TaskCreate, UserRole
from secagent.repositories.dag_repository import DagRepository
from secagent.repository import TaskRepository
from secagent.services.assignment_policy import (
    ROUTE_REASON_TEXT_ZH,
    AssignmentDecision,
)
from secagent.services.dag_scheduler import DagScheduler
from secagent.queue.fake import FakeJobQueue


def _actor(name: str, role: UserRole = UserRole.ANALYST) -> AuthenticatedUser:
    return AuthenticatedUser(id=str(uuid4()), username=name, role=role)


@pytest.fixture
def scheduler_env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'dag-scheduler.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    actor = _actor("alice")
    with factory() as session:
        session.add(
            UserRow(
                id=actor.id,
                username=actor.username,
                password_hash="unused",
                role=actor.role.value,
            )
        )
        session.commit()
    yield factory, actor
    engine.dispose()


def _budget() -> TurnBudgetSnapshot:
    return TurnBudgetSnapshot(
        max_subtasks=12,
        max_model_calls_per_subtask=2,
        max_tool_calls_per_subtask=4,
        timeout_seconds=60,
        max_replans=1,
        max_context_tokens=8_000,
    )


def _turn_with_subtasks(
    factory: sessionmaker[Session],
    actor: AuthenticatedUser,
    specs: list[tuple[str, list[str], bool]],
) -> str:
    with factory() as session:
        repository = ConversationRepository(session)
        conversation = repository.create_conversation(
            actor, ConversationCreate(title="Scheduler case")
        )
        message = repository.add_message(
            actor,
            conversation.id,
            ConversationMessageWrite(
                role="user", kind="user_text", content="调度测试", idempotency_key="k1"
            ),
        )
        task = TaskRepository(session).create_task(
            TaskCreate(goal="调度测试目标", authorization_scope="仅测试数据"),
            owner_id=conversation.owner_id,
            commit=False,
        )
        repository.commit()
        turn = repository.create_turn(
            actor,
            conversation.id,
            ConversationTurnCreate(
                trigger_message_id=message.message.id,
                task_id=task.id,
                budget=_budget(),
            ),
        )
        document = DecompositionDocument.model_validate(
            {
                "plan_version": turn.plan_version,
                "goal_summary": "调度测试目标",
                "subtasks": [
                    {
                        "key": key,
                        "title": f"任务 {key}",
                        "objective": f"执行 {key} 的目标。",
                        "dependency_keys": deps,
                        "required_capabilities": ["chinese_semantic"],
                        "proposed_provider": "glm",
                        "route_reason_code": "glm_chinese_strength",
                        "allowed_tools": [],
                        "expected_output": "输出",
                        "required": required,
                    }
                    for key, deps, required in specs
                ],
                "synthesis_requirements": ["区分事实和推断"],
            }
        )
        assignments = [
            AssignmentDecision(
                key=key,
                proposed_provider="glm",
                assigned_provider="glm",
                route_reason_code=RouteReasonCode.GLM_CHINESE_STRENGTH,
                route_reason=ROUTE_REASON_TEXT_ZH[
                    RouteReasonCode.GLM_CHINESE_STRENGTH
                ],
                allowed_tools=[],
                corrected=False,
                correction_codes=[],
            )
            for key, _deps, _required in specs
        ]
        DagRepository(session).create_subtasks_from_document(
            turn.id, document, assignments
        )
        DagRepository(session).mark_turn_state(
            turn.id, "scheduling", expected={"created"}
        )
        session.commit()
        return turn.id


def _scheduler(factory, queue: FakeJobQueue, max_parallel: int = 3) -> DagScheduler:
    return DagScheduler(
        session_factory=factory, queue=queue, max_parallel=max_parallel
    )


def _subtask_rows(factory, turn_id: str) -> dict[str, SubtaskRow]:
    with factory() as session:
        rows = session.scalars(
            select(SubtaskRow).where(SubtaskRow.turn_id == turn_id)
        ).all()
        session.expunge_all()
        return {row.key: row for row in rows}


def _jobs(factory, kind: JobKind) -> list[JobRunRow]:
    with factory() as session:
        rows = session.scalars(
            select(JobRunRow).where(JobRunRow.job_kind == kind.value)
        ).all()
        session.expunge_all()
        return rows


def test_schedule_respects_concurrency_cap_and_is_idempotent(scheduler_env) -> None:
    factory, actor = scheduler_env
    queue = FakeJobQueue()
    specs = [(f"task_{i}", [], True) for i in range(5)]
    turn_id = _turn_with_subtasks(factory, actor, specs)

    created = _scheduler(factory, queue, max_parallel=2).schedule_turn(turn_id)

    assert created == 2
    assert len(queue.dag_enqueued) == 2
    assert all(job.kind is JobKind.SUBTASK_EXECUTE for job in queue.dag_enqueued)
    rows = _subtask_rows(factory, turn_id)
    assert sum(1 for row in rows.values() if row.status == "queued") == 2

    with factory() as session:
        turn = session.get(ConversationTurnRow, turn_id)
        assert turn.status == "running"

    assert _scheduler(factory, queue, max_parallel=2).schedule_turn(turn_id) == 0
    assert len(queue.dag_enqueued) == 2


def test_on_subtask_finished_unlocks_dependencies(scheduler_env) -> None:
    factory, actor = scheduler_env
    queue = FakeJobQueue()
    turn_id = _turn_with_subtasks(
        factory, actor, [("first", [], True), ("second", ["first"], True)]
    )
    scheduler = _scheduler(factory, queue, max_parallel=3)

    assert scheduler.schedule_turn(turn_id) == 1
    rows = _subtask_rows(factory, turn_id)
    first, second = rows["first"], rows["second"]
    assert first.status == "queued"
    assert second.status == "pending_dependency"

    with factory() as session:
        dag = DagRepository(session)
        dag.transition_subtask(
            first.id,
            SubtaskStatus.RUNNING,
            expected={SubtaskStatus.QUEUED},
            reason="worker claimed",
        )
        dag.transition_subtask(
            first.id,
            SubtaskStatus.COMPLETED,
            expected={SubtaskStatus.RUNNING},
            reason="worker finished",
        )
        session.commit()

    assert scheduler.on_subtask_finished(first.id) == 1
    rows = _subtask_rows(factory, turn_id)
    assert rows["second"].status == "queued"


def test_required_failure_blocks_downstream_and_synthesis(scheduler_env) -> None:
    factory, actor = scheduler_env
    queue = FakeJobQueue()
    turn_id = _turn_with_subtasks(
        factory, actor, [("first", [], True), ("second", ["first"], True)]
    )
    scheduler = _scheduler(factory, queue, max_parallel=3)
    scheduler.schedule_turn(turn_id)

    first = _subtask_rows(factory, turn_id)["first"]
    with factory() as session:
        dag = DagRepository(session)
        dag.transition_subtask(
            first.id,
            SubtaskStatus.RUNNING,
            expected={SubtaskStatus.QUEUED},
            reason="worker claimed",
        )
        dag.transition_subtask(
            first.id,
            SubtaskStatus.FAILED,
            expected={SubtaskStatus.RUNNING},
            reason="worker failed",
        )
        session.commit()

    assert scheduler.on_subtask_finished(first.id) == 0
    rows = _subtask_rows(factory, turn_id)
    assert rows["second"].status == "pending_dependency"
    assert _jobs(factory, JobKind.TURN_SYNTHESIZE) == []


def test_all_required_terminal_creates_exactly_one_synthesize_job(
    scheduler_env,
) -> None:
    factory, actor = scheduler_env
    queue = FakeJobQueue()
    turn_id = _turn_with_subtasks(
        factory,
        actor,
        [("a", [], True), ("b", [], True), ("optional", [], False)],
    )
    scheduler = _scheduler(factory, queue, max_parallel=3)
    scheduler.schedule_turn(turn_id)

    with factory() as session:
        dag = DagRepository(session)
        for key in ("a", "b"):
            row = session.scalar(
                select(SubtaskRow).where(
                    SubtaskRow.turn_id == turn_id, SubtaskRow.key == key
                )
            )
            dag.transition_subtask(
                row.id,
                SubtaskStatus.COMPLETED,
                expected={SubtaskStatus.QUEUED, SubtaskStatus.RUNNING},
                reason="done",
            )
        session.commit()

    created = scheduler.on_subtask_finished(
        _subtask_rows(factory, turn_id)["a"].id
    )
    assert created == 1
    rows = _subtask_rows(factory, turn_id)
    assert rows["optional"].status == "skipped"
    synthesize_jobs = _jobs(factory, JobKind.TURN_SYNTHESIZE)
    assert len(synthesize_jobs) == 1

    assert (
        scheduler.schedule_turn(turn_id) == 0
    )  # second pass never duplicates the job
    assert len(_jobs(factory, JobKind.TURN_SYNTHESIZE)) == 1


def test_no_dispatch_when_turn_is_waiting_model_decision(scheduler_env) -> None:
    factory, actor = scheduler_env
    queue = FakeJobQueue()
    turn_id = _turn_with_subtasks(factory, actor, [("a", [], True)])

    with factory() as session:
        DagRepository(session).mark_turn_state(
            turn_id, "waiting_model_decision", expected={"scheduling"}
        )
        session.commit()

    assert _scheduler(factory, queue).schedule_turn(turn_id) == 0
    assert queue.dag_enqueued == []
    assert _jobs(factory, JobKind.SUBTASK_EXECUTE) == []


def test_duplicate_schedule_does_not_oversubscribe_slots(scheduler_env) -> None:
    factory, actor = scheduler_env
    queue = FakeJobQueue()
    turn_id = _turn_with_subtasks(
        factory, actor, [("a", [], True), ("b", [], True)]
    )
    scheduler = _scheduler(factory, queue, max_parallel=3)

    first = scheduler.schedule_turn(turn_id)
    second = scheduler.schedule_turn(turn_id)

    assert first == 2
    assert second == 0
    rows = _subtask_rows(factory, turn_id)
    assert {row.status for row in rows.values()} == {"queued"}


def test_transition_fails_when_expected_state_does_not_match(scheduler_env) -> None:
    factory, actor = scheduler_env
    queue = FakeJobQueue()
    turn_id = _turn_with_subtasks(factory, actor, [("a", [], True)])
    row = _subtask_rows(factory, turn_id)["a"]

    with factory() as session:
        with pytest.raises(SubtaskTransitionError):
            DagRepository(session).transition_subtask(
                row.id,
                SubtaskStatus.RUNNING,
                expected={SubtaskStatus.QUEUED},
                reason="pending row cannot be claimed as running",
            )
        session.rollback()

    with factory() as session:
        moved = DagRepository(session).transition_subtask(
            row.id,
            SubtaskStatus.QUEUED,
            expected={SubtaskStatus.PENDING_DEPENDENCY},
            reason="legal dispatch",
        )
        session.commit()
        assert moved.status is SubtaskStatus.QUEUED
