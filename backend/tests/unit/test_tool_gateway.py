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
from secagent.dag_domain import SubtaskStatus
from secagent.db import Base, make_engine
from secagent.db_models import (
    ApprovalRow,
    ConversationEventRow,
    EvidenceRow,
    ToolCallRow,
    UserRow,
)
from secagent.domain import RiskLevel, TaskCreate, ToolResult, UserRole
from secagent.repositories.dag_repository import DagRepository
from secagent.repository import TaskRepository
from secagent.services.ledger import LedgerService
from secagent.services.tool_gateway import ToolGateway
from secagent.tools.base import BaseTool, ToolContext
from secagent.tools.registry import ToolRegistry
from secagent.agents.executor import DemoEvidenceTool
from secagent.tools.source_tools import SourceScanner
from secagent.tools.http_request import HttpRequest
from secagent.security.url_guard import UrlGuard


def _actor(name: str, role: UserRole = UserRole.ANALYST) -> AuthenticatedUser:
    return AuthenticatedUser(id=str(uuid4()), username=name, role=role)


class _MediumProbe(BaseTool):
    """Deterministic MEDIUM-risk tool so the approval test needs no network."""

    name = "medium_probe"
    scene = "web_analysis"
    risk_level = RiskLevel.MEDIUM
    idempotent = True

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, params: dict, context: ToolContext) -> ToolResult:
        del params, context
        self.calls += 1
        return ToolResult(
            success=True,
            summary="probe ok",
            evidence=[
                {
                    "evidence_type": "http_observation",
                    "source": "probe",
                    "content": "ok",
                    "confidence": 1.0,
                }
            ],
        )


@pytest.fixture
def gateway_env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'tool-gateway.db'}")
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
            SourceScanner(),
            HttpRequest(UrlGuard({"example.test"})),
        ]
    )
    yield factory, actor, registry
    engine.dispose()


def _turn_and_subtask(
    factory: sessionmaker[Session],
    actor: AuthenticatedUser,
    *,
    allowed_tools: list[str],
    safety_mode: str = "conservative",
) -> tuple[str, str, str]:
    with factory() as session:
        repository = ConversationRepository(session)
        conversation = repository.create_conversation(
            actor,
            ConversationCreate(
                settings=ConversationSettings(
                    authorization_scope="仅测试数据",
                    safety_mode=safety_mode,
                )
            ),
        )
        message = repository.add_message(
            actor,
            conversation.id,
            ConversationMessageWrite(
                role="user", kind="user_text", content="网关测试", idempotency_key="k1"
            ),
        )
        task = TaskRepository(session).create_task(
            TaskCreate(goal="网关测试目标", authorization_scope="仅测试数据"),
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
                budget=TurnBudgetSnapshot(
                    max_subtasks=3,
                    max_model_calls_per_subtask=2,
                    max_tool_calls_per_subtask=4,
                    timeout_seconds=60,
                    max_replans=1,
                    max_context_tokens=8_000,
                ),
            ),
        )
        document = DecompositionDocument.model_validate(
            {
                "plan_version": turn.plan_version,
                "goal_summary": "网关测试目标",
                "subtasks": [
                    {
                        "key": "probe",
                        "title": "探测",
                        "objective": "执行探测。",
                        "dependency_keys": [],
                        "required_capabilities": ["tool_request"],
                        "proposed_provider": "glm",
                        "route_reason_code": "tool_compatible",
                        "allowed_tools": allowed_tools,
                        "expected_output": "工具证据",
                        "required": True,
                    }
                ],
                "synthesis_requirements": ["区分事实和推断"],
            }
        )
        from secagent.services.assignment_policy import (
            ROUTE_REASON_TEXT_ZH,
            AssignmentDecision,
        )
        from secagent.conversation_decomposition import RouteReasonCode

        dag = DagRepository(session)
        subtasks = dag.create_subtasks_from_document(
            turn.id,
            document,
            [
                AssignmentDecision(
                    key="probe",
                    proposed_provider="glm",
                    assigned_provider="glm",
                    route_reason_code=RouteReasonCode.TOOL_COMPATIBLE,
                    route_reason=ROUTE_REASON_TEXT_ZH[RouteReasonCode.TOOL_COMPATIBLE],
                    allowed_tools=allowed_tools,
                    corrected=False,
                    correction_codes=[],
                )
            ],
        )
        session.commit()
        return turn.id, task.id, subtasks[0].id


def _mark_running(factory, subtask_id: str) -> str:
    """Create the attempt and flip the subtask to running; returns attempt id."""
    with factory() as session:
        dag = DagRepository(session)
        attempt = dag.create_attempt(
            subtask_id,
            provider="glm",
            model="glm-5.2",
            idempotency_key=f"gw-{subtask_id}",
        )
        dag.transition_subtask(
            subtask_id,
            SubtaskStatus.RUNNING,
            expected={SubtaskStatus.PENDING_DEPENDENCY, SubtaskStatus.QUEUED},
            reason="test",
        )
        session.commit()
        return attempt.id


def _build_gateway(
    factory,
    registry,
    turn_id: str,
    task_id: str,
    subtask_id: str,
    attempt_id: str,
    allowed_tools: list[str],
    workspace,
    safety_mode: str = "conservative",
) -> ToolGateway:
    session = factory()
    return ToolGateway(
        registry=registry,
        ledger=LedgerService(TaskRepository(session)),
        dag=DagRepository(session),
        task_repository=TaskRepository(session),
        workspace=workspace,
        safety_mode=safety_mode,
        allowed_tools=allowed_tools,
        task_id=task_id,
        turn_id=turn_id,
        subtask_id=subtask_id,
        attempt_id=attempt_id,
    )


async def test_gateway_rejects_unregistered_and_unallowed_tools(gateway_env) -> None:
    factory, actor, registry = gateway_env
    turn_id, task_id, subtask_id = _turn_and_subtask(
        factory, actor, allowed_tools=["demo_evidence"]
    )
    attempt_id = _mark_running(factory, subtask_id)
    gateway = _build_gateway(
        factory,
        registry,
        turn_id,
        task_id,
        subtask_id,
        attempt_id,
        allowed_tools=["demo_evidence"],
        workspace=None,
    )

    rejected_unregistered = await gateway.submit(
        subtask_key="probe", tool_name="no_such_tool", params={}
    )
    assert rejected_unregistered.status == "rejected"

    rejected_unallowed = await gateway.submit(
        subtask_key="probe", tool_name="source_scanner", params={}
    )
    assert rejected_unallowed.status == "rejected"
    gateway.dag.session.commit()

    with factory() as session:
        events = session.scalars(
            select(ConversationEventRow).where(
                ConversationEventRow.event_type == "subtask.tool.rejected"
            )
        ).all()
        assert len(events) == 2


async def test_gateway_executes_low_risk_tool_with_linkage(gateway_env, tmp_path) -> None:
    factory, actor, registry = gateway_env
    turn_id, task_id, subtask_id = _turn_and_subtask(
        factory, actor, allowed_tools=["demo_evidence"]
    )
    attempt_id = _mark_running(factory, subtask_id)
    gateway = _build_gateway(
        factory,
        registry,
        turn_id,
        task_id,
        subtask_id,
        attempt_id,
        allowed_tools=["demo_evidence"],
        workspace=tmp_path / "workspace",
    )

    outcome = await gateway.submit(
        subtask_key="probe", tool_name="demo_evidence", params={}
    )

    assert outcome.status == "executed"
    assert outcome.result.success is True
    gateway.dag.session.commit()
    with factory() as session:
        calls = session.scalars(select(ToolCallRow)).all()
        assert len(calls) == 1
        assert calls[0].subtask_id == subtask_id
        assert calls[0].turn_id == turn_id
        evidences = session.scalars(select(EvidenceRow)).all()
        assert len(evidences) == 1
        assert evidences[0].source.startswith("probe:")
        assert evidences[0].subtask_id == subtask_id


async def test_gateway_routes_medium_risk_tool_to_approval(gateway_env) -> None:
    factory, actor, registry = gateway_env
    probe = _MediumProbe()
    registry.register(probe)
    turn_id, task_id, subtask_id = _turn_and_subtask(
        factory, actor, allowed_tools=["medium_probe"], safety_mode="conservative"
    )
    attempt_id = _mark_running(factory, subtask_id)
    gateway = _build_gateway(
        factory,
        registry,
        turn_id,
        task_id,
        subtask_id,
        attempt_id,
        allowed_tools=["medium_probe"],
        workspace=None,
    )

    outcome = await gateway.submit(
        subtask_key="probe",
        tool_name="medium_probe",
        params={"url": "https://example.test/", "api_key": "sk-secret-123456"},
    )

    assert outcome.status == "wait"
    gateway.dag.session.commit()
    with factory() as session:
        approvals = session.scalars(select(ApprovalRow)).all()
        assert len(approvals) == 1
        assert approvals[0].subtask_id == subtask_id
        assert approvals[0].step_id is None
        assert "sk-secret-123456" not in approvals[0].params_summary
        events = session.scalars(
            select(ConversationEventRow).where(
                ConversationEventRow.event_type == "subtask.waiting_approval"
            )
        ).all()
        assert len(events) == 1


async def test_gateway_rejects_forbidden_risk_tool(gateway_env) -> None:
    from secagent.domain import RiskLevel

    factory, actor, registry = gateway_env
    turn_id, task_id, subtask_id = _turn_and_subtask(
        factory, actor, allowed_tools=["source_scanner"]
    )
    attempt_id = _mark_running(factory, subtask_id)
    gateway = _build_gateway(
        factory,
        registry,
        turn_id,
        task_id,
        subtask_id,
        attempt_id,
        allowed_tools=["source_scanner"],
        workspace=None,
    )

    source_scanner = registry.get("source_scanner")
    original = source_scanner.risk_level
    source_scanner.risk_level = RiskLevel.FORBIDDEN
    try:
        outcome = await gateway.submit(
            subtask_key="probe", tool_name="source_scanner", params={}
        )
    finally:
        source_scanner.risk_level = original
    assert outcome.status == "rejected"
    gateway.dag.session.commit()


async def test_gateway_honours_approved_medium_risk_tool(gateway_env, tmp_path) -> None:
    """An approved medium tool executes on the retry instead of re-entering the
    approval queue forever (the conversational-subtask approval bug)."""
    factory, actor, registry = gateway_env
    probe = _MediumProbe()
    registry.register(probe)
    turn_id, task_id, subtask_id = _turn_and_subtask(
        factory, actor, allowed_tools=["medium_probe"], safety_mode="conservative"
    )
    attempt_id = _mark_running(factory, subtask_id)
    gateway = _build_gateway(
        factory,
        registry,
        turn_id,
        task_id,
        subtask_id,
        attempt_id,
        allowed_tools=["medium_probe"],
        workspace=tmp_path / "workspace",
    )

    # First request: medium risk with no prior approval -> waits.
    first = await gateway.submit(
        subtask_key="probe", tool_name="medium_probe", params={}
    )
    assert first.status == "wait"
    assert probe.calls == 0
    gateway.dag.session.commit()

    # Simulate the operator approving the created approval.
    with factory() as session:
        approval = session.scalars(select(ApprovalRow)).one()
        approval.status = "approved"
        session.commit()

    # Build a fresh gateway on a fresh session (the retry loop does the same),
    # so the approved approval is visible. It must honour it and actually run.
    retry = _build_gateway(
        factory,
        registry,
        turn_id,
        task_id,
        subtask_id,
        attempt_id,
        allowed_tools=["medium_probe"],
        workspace=tmp_path / "workspace",
    )
    second = await retry.submit(
        subtask_key="probe", tool_name="medium_probe", params={}
    )
    assert second.status == "executed"
    assert second.result is not None and second.result.success is True
    assert probe.calls == 1
    retry.dag.session.commit()

    with factory() as session:
        calls = session.scalars(select(ToolCallRow)).all()
        assert len(calls) == 1
        assert calls[0].tool_name == "medium_probe"
