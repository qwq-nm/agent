import asyncio
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
from secagent.config import Settings
from secagent.dag_domain import (
    JobKind,
    SubtaskStatus,
    subtask_execute_command_id,
)
from secagent.db import Base, make_engine
from secagent.db_models import (
    ApprovalRow,
    ConversationTurnRow,
    JobRunRow,
    ModelCallRow,
    ModelFailureRow,
    SubtaskAttemptRow,
    SubtaskResultRow,
    SubtaskRow,
    ToolCallRow,
    UserRow,
)
from secagent.domain import TaskCreate, UserRole
from secagent.agents.executor import DemoEvidenceTool
from secagent.repositories.dag_repository import DagRepository
from secagent.repository import TaskRepository
from secagent.services.assignment_policy import (
    ROUTE_REASON_TEXT_ZH,
    AssignmentDecision,
)
from secagent.conversation_decomposition import RouteReasonCode
from secagent.services.subtask_runner import execute_subtask_job
from secagent.tools.http_request import HttpRequest
from secagent.tools.registry import ToolRegistry
from secagent.security.url_guard import UrlGuard


def _actor(name: str, role: UserRole = UserRole.ANALYST) -> AuthenticatedUser:
    return AuthenticatedUser(id=str(uuid4()), username=name, role=role)


@pytest.fixture
def runner_env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'subtask-runner.db'}")
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
    registry = ToolRegistry(
        [
            DemoEvidenceTool(),
            HttpRequest(UrlGuard({"example.test"})),
        ]
    )
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'subtask-runner.db'}",
        data_dir=tmp_path / "data",
        model_mode="mock",
        jwt_signing_key="test-signing-key-at-least-32-bytes",
    )
    yield factory, actor, registry, settings
    engine.dispose()


def _make_subtask(
    factory: sessionmaker[Session],
    actor: AuthenticatedUser,
    *,
    allowed_tools: list[str],
    budget: TurnBudgetSnapshot | None = None,
    safety_mode: str = "conservative",
    assigned_provider: str = "glm",
) -> tuple[str, str]:
    with factory() as session:
        repository = ConversationRepository(session)
        conversation = repository.create_conversation(
            actor,
            ConversationCreate(
                settings=ConversationSettings(
                    authorization_scope="仅测试数据", safety_mode=safety_mode
                )
            ),
        )
        message = repository.add_message(
            actor,
            conversation.id,
            ConversationMessageWrite(
                role="user", kind="user_text", content="运行子任务", idempotency_key="k1"
            ),
        )
        task = TaskRepository(session).create_task(
            TaskCreate(goal="子任务测试目标", authorization_scope="仅测试数据"),
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
                budget=budget
                or TurnBudgetSnapshot(
                    max_subtasks=2,
                    max_model_calls_per_subtask=4,
                    max_tool_calls_per_subtask=4,
                    timeout_seconds=60,
                    max_replans=1,
                    max_context_tokens=8_000,
                ),
            ),
        )
        repository.mark_turn_state_for_test = None
        DagRepository(session).mark_turn_state(
            turn.id, "running", expected={"created"}
        )
        document = DecompositionDocument.model_validate(
            {
                "plan_version": turn.plan_version,
                "goal_summary": "子任务测试目标",
                "subtasks": [
                    {
                        "key": "probe",
                        "title": "探测",
                        "objective": "执行探测并给出结论。",
                        "dependency_keys": [],
                        "required_capabilities": ["tool_request"],
                        "proposed_provider": assigned_provider,
                        "route_reason_code": "tool_compatible",
                        "allowed_tools": allowed_tools,
                        "expected_output": "工具证据与结论",
                        "required": True,
                    }
                ],
                "synthesis_requirements": ["区分事实和推断"],
            }
        )
        dag = DagRepository(session)
        subtask = dag.create_subtasks_from_document(
            turn.id,
            document,
            [
                AssignmentDecision(
                    key="probe",
                    proposed_provider=assigned_provider,
                    assigned_provider=assigned_provider,
                    route_reason_code=RouteReasonCode.TOOL_COMPATIBLE,
                    route_reason=ROUTE_REASON_TEXT_ZH[
                        RouteReasonCode.TOOL_COMPATIBLE
                    ],
                    allowed_tools=allowed_tools,
                    corrected=False,
                    correction_codes=[],
                )
            ],
        )[0]
        command_id = subtask_execute_command_id(subtask.id, 1)
        job_row = TaskRepository(session).add_job_run(task.id, command_id)
        job_row.job_kind = JobKind.SUBTASK_EXECUTE.value
        job_row.turn_id = turn.id
        job_row.subtask_id = subtask.id
        job_row.status = "queued"
        session.flush()
        dag.transition_subtask(
            subtask.id,
            SubtaskStatus.QUEUED,
            expected={SubtaskStatus.PENDING_DEPENDENCY},
            reason="test dispatch",
        )
        session.commit()
        return subtask.id, command_id


def _run(factory, registry, settings, subtask_id: str, command_id: str) -> None:
    asyncio.run(
        execute_subtask_job(
            subtask_id, command_id, factory, registry, settings
        )
    )


def _status(factory, subtask_id: str) -> str:
    with factory() as session:
        return session.get(SubtaskRow, subtask_id).status


def test_mock_subtask_completes_with_tool_evidence(runner_env) -> None:
    factory, actor, registry, settings = runner_env
    subtask_id, command_id = _make_subtask(
        factory, actor, allowed_tools=["demo_evidence"]
    )

    _run(factory, registry, settings, subtask_id, command_id)

    with factory() as session:
        subtask = session.get(SubtaskRow, subtask_id)
        assert subtask.status == "completed"
        assert subtask.result_summary

        results = session.scalars(
            select(SubtaskResultRow).where(SubtaskResultRow.subtask_id == subtask_id)
        ).all()
        assert len(results) == 1
        assert results[0].status == "completed"
        assert "demo_evidence:result" in results[0].evidence_refs_json

        calls = session.scalars(
            select(ModelCallRow).where(ModelCallRow.subtask_id == subtask_id)
        ).all()
        assert len(calls) == 2  # tool request + final result
        assert all(call.stage == "subtask_execute" for call in calls)
        assert all(call.provider == "mock" for call in calls)
        assert all(call.turn_id == subtask.turn_id for call in calls)

        tool_calls = session.scalars(select(ToolCallRow)).all()
        assert len(tool_calls) == 1
        assert tool_calls[0].tool_name == "demo_evidence"
        assert tool_calls[0].subtask_id == subtask_id

        attempts = session.scalars(
            select(SubtaskAttemptRow).where(
                SubtaskAttemptRow.subtask_id == subtask_id
            )
        ).all()
        assert len(attempts) == 1
        assert attempts[0].status == "completed"

        job = session.scalar(
            select(JobRunRow).where(JobRunRow.command_id == command_id)
        )
        assert job.status == "completed"


def test_zero_model_budget_yields_incomplete(runner_env) -> None:
    factory, actor, registry, settings = runner_env
    subtask_id, command_id = _make_subtask(
        factory,
        actor,
        allowed_tools=[],
        budget=TurnBudgetSnapshot(
            max_subtasks=2,
            max_model_calls_per_subtask=0,
            max_tool_calls_per_subtask=4,
            timeout_seconds=60,
            max_replans=1,
            max_context_tokens=8_000,
        ),
    )

    _run(factory, registry, settings, subtask_id, command_id)

    assert _status(factory, subtask_id) == "incomplete"


def test_transport_failure_records_model_failure_and_waits(runner_env) -> None:
    factory, actor, registry, settings = runner_env
    live_settings = settings.model_copy(update={"model_mode": "live"})
    subtask_id, command_id = _make_subtask(
        factory, actor, allowed_tools=[], assigned_provider="deepseek"
    )

    _run(factory, registry, live_settings, subtask_id, command_id)

    with factory() as session:
        assert session.get(SubtaskRow, subtask_id).status == "waiting_model_decision"
        failure = session.scalar(
            select(ModelFailureRow).where(ModelFailureRow.subtask_id == subtask_id)
        )
        assert failure is not None
        assert failure.stage == "subtask"
        assert failure.status == "waiting_decision"
        assert failure.decision is None
        job = session.scalar(
            select(JobRunRow).where(JobRunRow.command_id == command_id)
        )
        assert job.status == "failed"


def test_medium_tool_request_pauses_for_approval(runner_env) -> None:
    factory, actor, registry, settings = runner_env
    subtask_id, command_id = _make_subtask(
        factory, actor, allowed_tools=["http_fetch"], safety_mode="conservative"
    )

    _run(factory, registry, settings, subtask_id, command_id)

    with factory() as session:
        assert (
            session.get(SubtaskRow, subtask_id).status == "waiting_tool_approval"
        )
        approvals = session.scalars(select(ApprovalRow)).all()
        assert len(approvals) == 1
        assert approvals[0].subtask_id == subtask_id
        assert approvals[0].turn_id is not None
        job = session.scalar(
            select(JobRunRow).where(JobRunRow.command_id == command_id)
        )
        assert job.status == "completed"
        assert (
            session.scalars(
                select(SubtaskResultRow).where(
                    SubtaskResultRow.subtask_id == subtask_id
                )
            ).all()
            == []
        )


def test_turn_replan_cancels_a_running_subtask_at_checkpoint(runner_env) -> None:
    factory, actor, registry, settings = runner_env
    subtask_id, command_id = _make_subtask(
        factory, actor, allowed_tools=["demo_evidence"]
    )
    with factory() as session:
        turn_row = session.get(SubtaskRow, subtask_id)
        turn_id = turn_row.turn_id
        session.get(ConversationTurnRow, turn_id)
        session.execute(
            __import__("sqlalchemy").update(ConversationTurnRow)
            .where(ConversationTurnRow.id == turn_id)
            .values(status="replan_requested")
        )
        session.commit()

    _run(factory, registry, settings, subtask_id, command_id)

    assert _status(factory, subtask_id) == "superseded"
