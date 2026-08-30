from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from secagent.auth.dependencies import AuthenticatedUser
from secagent.conversation_decomposition import DecompositionDocument
from secagent.conversation_domain import (
    ConversationCreate,
    ConversationMessageWrite,
    ConversationSettings,
    ConversationTurnCreate,
    TurnBudgetSnapshot,
)
from secagent.conversation_repository import ConversationRepository
from secagent.dag_domain import (
    ModelFailureAlreadyResolved,
    ModelFailureCreate,
    ModelFailureDecision,
    SubtaskStatus,
    SubtaskTransitionError,
)
from secagent.db import Base, make_engine
from secagent.db_models import ConversationEventRow, UserRow
from secagent.domain import UserRole
from secagent.repositories.dag_repository import DagRepository
from secagent.services.assignment_policy import (
    ROUTE_REASON_TEXT_ZH,
    AssignmentDecision,
)
from secagent.conversation_decomposition import RouteReasonCode


def _actor(name: str, role: UserRole = UserRole.ANALYST) -> AuthenticatedUser:
    return AuthenticatedUser(id=str(uuid4()), username=name, role=role)


@pytest.fixture
def dag_env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'dag-repository.db'}")
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


def _turn_id(factory: sessionmaker[Session], actor: AuthenticatedUser) -> str:
    with factory() as session:
        repository = ConversationRepository(session)
        conversation = repository.create_conversation(
            actor, ConversationCreate(title="DAG case")
        )
        message = repository.add_message(
            actor,
            conversation.id,
            ConversationMessageWrite(
                role="user", kind="user_text", content="分析日志", idempotency_key="k1"
            ),
        )
        turn = repository.create_turn(
            actor,
            conversation.id,
            ConversationTurnCreate(
                trigger_message_id=message.message.id, budget=_budget()
            ),
        )
        return turn.id


def _budget() -> TurnBudgetSnapshot:
    return TurnBudgetSnapshot(
        max_subtasks=3,
        max_model_calls_per_subtask=2,
        max_tool_calls_per_subtask=4,
        timeout_seconds=60,
        max_replans=1,
        max_context_tokens=8_000,
    )


def _document() -> DecompositionDocument:
    return DecompositionDocument.model_validate(
        {
            "plan_version": 1,
            "goal_summary": "分析材料",
            "subtasks": [
                {
                    "key": "extract_context",
                    "title": "提炼材料",
                    "objective": "提取关键事实。",
                    "dependency_keys": [],
                    "required_capabilities": ["chinese_semantic"],
                    "proposed_provider": "glm",
                    "route_reason_code": "glm_chinese_strength",
                    "allowed_tools": [],
                    "expected_output": "事实摘要",
                    "required": True,
                },
                {
                    "key": "assess_risk",
                    "title": "评估风险",
                    "objective": "基于事实评估风险。",
                    "dependency_keys": ["extract_context"],
                    "required_capabilities": ["code_security_reasoning"],
                    "proposed_provider": "deepseek",
                    "route_reason_code": "deepseek_code_security_strength",
                    "allowed_tools": ["source_scanner"],
                    "expected_output": "风险列表",
                    "required": True,
                },
            ],
            "synthesis_requirements": ["区分事实和推断"],
        }
    )


def _assignments() -> list[AssignmentDecision]:
    def decision(key: str, provider: str, reason_code: RouteReasonCode) -> AssignmentDecision:
        return AssignmentDecision(
            key=key,
            proposed_provider=provider,
            assigned_provider=provider,
            route_reason_code=reason_code,
            route_reason=ROUTE_REASON_TEXT_ZH[reason_code],
            allowed_tools=["source_scanner"] if key == "assess_risk" else [],
            corrected=False,
            correction_codes=[],
        )

    return [
        decision("extract_context", "glm", RouteReasonCode.GLM_CHINESE_STRENGTH),
        decision(
            "assess_risk", "deepseek", RouteReasonCode.DEEPSEEK_CODE_SECURITY_STRENGTH
        ),
    ]


def test_create_persists_rows_dependencies_and_assigned_events(dag_env) -> None:
    factory, actor = dag_env
    turn_id = _turn_id(factory, actor)
    with factory() as session:
        repository = DagRepository(session)
        subtasks = repository.create_subtasks_from_document(
            turn_id, _document(), _assignments()
        )
        repository.commit()

        assert {item.key for item in subtasks} == {
            "extract_context",
            "assess_risk",
        }
        assert all(
            item.status is SubtaskStatus.PENDING_DEPENDENCY for item in subtasks
        )
        by_key = {item.key: item for item in subtasks}
        assert by_key["extract_context"].assigned_provider == "glm"
        assert by_key["assess_risk"].allowed_tools == ["source_scanner"]

        events = session.scalars(
            select(ConversationEventRow).where(
                ConversationEventRow.event_type == "subtask.assigned"
            )
        ).all()
        assert len(events) == 2


def test_dependency_rows_are_written_once_per_edge(dag_env) -> None:
    factory, actor = dag_env
    turn_id = _turn_id(factory, actor)
    with factory() as session:
        from secagent.db_models import SubtaskDependencyRow, SubtaskRow

        repository = DagRepository(session)
        repository.create_subtasks_from_document(turn_id, _document(), _assignments())
        repository.commit()

        rows = session.execute(
            select(SubtaskDependencyRow.subtask_id, SubtaskDependencyRow.dependency_subtask_id)
        ).all()
        assert len(rows) == 1
        child = session.scalar(
            select(SubtaskRow).where(SubtaskRow.key == "assess_risk")
        )
        parent = session.scalar(
            select(SubtaskRow).where(SubtaskRow.key == "extract_context")
        )
        assert rows[0] == (child.id, parent.id)


def test_duplicate_creation_is_rejected(dag_env) -> None:
    factory, actor = dag_env
    turn_id = _turn_id(factory, actor)
    with factory() as session:
        repository = DagRepository(session)
        repository.create_subtasks_from_document(turn_id, _document(), _assignments())
        repository.commit()
        with pytest.raises(ValueError):
            repository.create_subtasks_from_document(
                turn_id, _document(), _assignments()
            )


def test_transitions_are_conditional_and_paired_with_events(dag_env) -> None:
    factory, actor = dag_env
    turn_id = _turn_id(factory, actor)
    with factory() as session:
        repository = DagRepository(session)
        created = repository.create_subtasks_from_document(
            turn_id, _document(), _assignments()
        )
        repository.commit()
        subtask_id = created[0].id

        queued = repository.transition_subtask(
            subtask_id,
            SubtaskStatus.QUEUED,
            expected={SubtaskStatus.PENDING_DEPENDENCY},
            reason="dependencies satisfied",
        )
        repository.commit()
        assert queued.status is SubtaskStatus.QUEUED

        with pytest.raises(SubtaskTransitionError):
            repository.transition_subtask(
                subtask_id,
                SubtaskStatus.RUNNING,
                expected={SubtaskStatus.PENDING_DEPENDENCY},
                reason="wrong expectation",
            )

        started = repository.transition_subtask(
            subtask_id,
            SubtaskStatus.RUNNING,
            expected={SubtaskStatus.QUEUED},
            reason="job claimed",
        )
        repository.commit()
        assert started.status is SubtaskStatus.RUNNING
        assert started.status_version == queued.status_version + 1

        events = session.scalars(
            select(ConversationEventRow)
            .where(ConversationEventRow.subtask_id == subtask_id)
            .order_by(ConversationEventRow.id.asc())
        ).all()
        assert [item.event_type for item in events] == [
            "subtask.assigned",
            "subtask.queued",
            "subtask.started",
        ]


def test_ready_subtasks_respect_completed_dependencies(dag_env) -> None:
    factory, actor = dag_env
    turn_id = _turn_id(factory, actor)
    with factory() as session:
        repository = DagRepository(session)
        created = repository.create_subtasks_from_document(
            turn_id, _document(), _assignments()
        )
        by_key = {item.key: item for item in created}
        repository.commit()

        assert repository.ready_subtasks(turn_id) == [by_key["extract_context"]]

        repository.transition_subtask(
            by_key["extract_context"].id,
            SubtaskStatus.QUEUED,
            expected={SubtaskStatus.PENDING_DEPENDENCY},
            reason="ready",
        )
        repository.transition_subtask(
            by_key["extract_context"].id,
            SubtaskStatus.COMPLETED,
            expected={SubtaskStatus.QUEUED, SubtaskStatus.RUNNING},
            reason="done",
        )
        repository.commit()
        assert [item.key for item in repository.ready_subtasks(turn_id)] == [
            "assess_risk"
        ]

        # A weaker but usable terminal result still unlocks dependents.
        repository.transition_subtask(
            by_key["extract_context"].id,
            SubtaskStatus.INCOMPLETE,
            expected={SubtaskStatus.COMPLETED},
            reason="downgraded to incomplete",
        )
        repository.commit()
        assert [item.key for item in repository.ready_subtasks(turn_id)] == [
            "assess_risk"
        ]


def test_attempts_are_idempotent_by_key_and_increment_by_subtask(dag_env) -> None:
    factory, actor = dag_env
    turn_id = _turn_id(factory, actor)
    with factory() as session:
        repository = DagRepository(session)
        created = repository.create_subtasks_from_document(
            turn_id, _document(), _assignments()
        )
        subtask_id = created[0].id
        repository.commit()

        first = repository.create_attempt(
            subtask_id,
            provider="glm",
            model="glm-5.2",
            idempotency_key="secagent:subtask:x:execute:a1",
        )
        again = repository.create_attempt(
            subtask_id,
            provider="glm",
            model="glm-5.2",
            idempotency_key="secagent:subtask:x:execute:a1",
        )
        second = repository.create_attempt(
            subtask_id,
            provider="glm",
            model="glm-5.2",
            idempotency_key="secagent:subtask:x:execute:a2",
        )
        repository.commit()
        assert first.id == again.id
        assert second.attempt == first.attempt + 1

        finished = repository.finish_attempt(
            first.id, status="completed", error_code=None
        )
        repository.commit()
        assert finished.status == "completed"


def test_save_subtask_result_upserts_and_sets_summary(dag_env) -> None:
    from secagent.dag_domain import SubtaskResultDocument

    factory, actor = dag_env
    turn_id = _turn_id(factory, actor)
    with factory() as session:
        repository = DagRepository(session)
        created = repository.create_subtasks_from_document(
            turn_id, _document(), _assignments()
        )
        subtask_id = created[0].id
        attempt = repository.create_attempt(
            subtask_id,
            provider="glm",
            model="glm-5.2",
            idempotency_key="key-1",
        )
        repository.commit()

        result = SubtaskResultDocument.model_validate(
            {
                "status": "completed",
                "summary": "第一版结论",
                "claims": [
                    {"statement": "事实一", "evidence_ref": "ev-1"},
                    {"statement": "承接上游", "upstream_key": "extract_context"},
                ],
                "evidence_refs": ["ev-1"],
            }
        )
        stored = repository.save_subtask_result(attempt.id, result)
        repository.commit()
        assert stored.summary == "第一版结论"
        assert len(stored.claims) == 2

        replacement = SubtaskResultDocument.model_validate(
            {
                "status": "incomplete",
                "summary": "第二版结论",
                "claims": [],
                "unresolved": ["缺证据"],
            }
        )
        updated = repository.save_subtask_result(attempt.id, replacement)
        repository.commit()
        assert updated.status == "incomplete"
        refreshed = repository.get_subtask(subtask_id)
        assert refreshed is not None
        assert refreshed.result_summary == "第二版结论"
        assert repository.latest_result(subtask_id).id == updated.id


def test_model_failure_resolution_is_single_shot(dag_env) -> None:
    factory, actor = dag_env
    turn_id = _turn_id(factory, actor)
    with factory() as session:
        repository = DagRepository(session)
        failure = repository.record_model_failure(
            ModelFailureCreate(
                turn_id=turn_id,
                subtask_id=None,
                stage="decompose",
                provider="deepseek",
                model="deepseek-v4-flash",
                error_code="provider_unavailable",
                detail="DeepSeek 未配置",
            )
        )
        repository.commit()
        assert failure.conversation_id
        assert failure.subtask_id is None
        assert failure.decision is None

    with factory() as session:
        repository = DagRepository(session)
        resolved = repository.resolve_model_failure(
            failure.id,
            decision=ModelFailureDecision.RETRY_SAME,
            decided_by=actor.id,
        )
        repository.commit()
        assert resolved.decision is ModelFailureDecision.RETRY_SAME
        with pytest.raises(ModelFailureAlreadyResolved):
            repository.resolve_model_failure(
                failure.id,
                decision=ModelFailureDecision.TERMINATE_TURN,
                decided_by=actor.id,
            )


def test_mark_turn_state_is_conditional(dag_env) -> None:
    factory, actor = dag_env
    turn_id = _turn_id(factory, actor)
    with factory() as session:
        repository = DagRepository(session)
        running = repository.mark_turn_state(
            turn_id, "running", expected={"created", "scheduling"}
        )
        repository.commit()
        assert running.status == "running"
        assert running.started_at is not None

        with pytest.raises(SubtaskTransitionError):
            repository.mark_turn_state(
                turn_id, "completed", expected={"scheduling"}
            )


def test_running_subtask_count_is_conversation_scoped(dag_env) -> None:
    from secagent.db_models import ConversationTurnRow

    factory, actor = dag_env
    turn_id = _turn_id(factory, actor)
    with factory() as session:
        repository = DagRepository(session)
        created = repository.create_subtasks_from_document(
            turn_id, _document(), _assignments()
        )
        for item in created:
            repository.transition_subtask(
                item.id,
                SubtaskStatus.QUEUED,
                expected={SubtaskStatus.PENDING_DEPENDENCY},
                reason="ready",
            )
            repository.transition_subtask(
                item.id,
                SubtaskStatus.RUNNING,
                expected={SubtaskStatus.QUEUED},
                reason="claimed",
            )
        repository.commit()
        conversation_id = session.get(ConversationTurnRow, turn_id).conversation_id
        assert repository.running_subtask_count(conversation_id) == 2
