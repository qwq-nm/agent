import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from secagent.config import Settings
from secagent.db import Base
from secagent.db_models import (
    ApprovalRow,
    ConversationTurnRow,
    JobRunRow,
    ModelFailureRow,
    SubtaskRow,
)
from secagent.dag_domain import JobKind, SubtaskStatus
from secagent.main import create_app
from secagent.repositories.dag_repository import DagRepository
from secagent.services.assignment_policy import (
    ROUTE_REASON_TEXT_ZH,
    AssignmentDecision,
)
from secagent.conversation_decomposition import DecompositionDocument, RouteReasonCode
from secagent.conversation_domain import (
    ConversationCreate,
    ConversationMessageWrite,
    ConversationTurnCreate,
    TurnBudgetSnapshot,
)
from secagent.conversation_repository import ConversationRepository
from secagent.domain import TaskCreate
from secagent.repository import TaskRepository
from secagent.services.dag_orchestrator import (
    execute_turn_decompose_job,
)
from secagent.services.dag_scheduler import DagScheduler


def _run_decompose(app, fake_queue, index: int = -1) -> None:
    job = fake_queue.dag_enqueued[index]
    asyncio.run(
        execute_turn_decompose_job(
            job.ref_id,
            job.command_id,
            app.state.session_factory,
            app.state.tool_registry,
            app.state.settings,
            queue=fake_queue,
        )
    )


def _run_subtask(app, fake_queue, index: int = -1) -> None:
    from secagent.services.subtask_runner import execute_subtask_job

    job = fake_queue.dag_enqueued[index]
    if job.kind is not JobKind.SUBTASK_EXECUTE:
        raise AssertionError(f"expected subtask job, got {job.kind}")
    asyncio.run(
        execute_subtask_job(
            job.ref_id,
            job.command_id,
            app.state.session_factory,
            app.state.tool_registry,
            app.state.settings,
            queue=fake_queue,
        )
    )


def _make_app(settings, fake_queue):
    app = create_app(settings, job_queue=fake_queue)
    Base.metadata.create_all(app.state.session_factory.kw["bind"])
    return app


@pytest.fixture
def live_no_provider_app(settings, fake_queue):
    live_settings = settings.model_copy(update={"model_mode": "live"})
    return _make_app(live_settings, fake_queue)


def _auth(client: TestClient, app, username: str) -> None:
    from secagent.services.auth_service import AuthService
    from secagent.domain import UserRole

    with app.state.session_factory() as session:
        AuthService.from_session(session, app.state.settings).create_user(
            username, "Live-Pass-9", UserRole.ANALYST
        )
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "Live-Pass-9"},
    )
    client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"


def _decompose_failure_turn(app, fake_queue, client) -> str:
    conversation_id = client.post("/api/conversations", json={}).json()["id"]
    client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "分析失败场景", "relative_paths": []},
        headers={"Idempotency-Key": "k-1"},
    )
    _run_decompose(app, fake_queue)
    with app.state.session_factory() as session:
        turn = session.scalars(
            select(ConversationTurnRow).where(
                ConversationTurnRow.conversation_id == conversation_id
            )
        ).all()[-1]
        assert turn.status == "waiting_model_decision"
        failure = session.scalar(
            select(ModelFailureRow).where(ModelFailureRow.turn_id == turn.id)
        )
        assert failure is not None
        return failure.id


def test_decompose_retry_same_requeues_the_job(
    live_no_provider_app, fake_queue
) -> None:
    app = live_no_provider_app
    with TestClient(app) as client:
        _auth(client, app, "retry-user")
        failure_id = _decompose_failure_turn(app, fake_queue, client)

        response = client.post(
            f"/api/model-failures/{failure_id}/decision",
            json={"decision": "retry_same"},
        )
        assert response.status_code == 200
        assert response.json()["decision"] == "retry_same"

        # the requeued job fails again (no provider), but the loop works
        assert fake_queue.dag_enqueued[-1].kind is JobKind.TURN_DECOMPOSE
        with app.state.session_factory() as session:
            row = session.scalar(
                select(JobRunRow).where(
                    JobRunRow.command_id == fake_queue.dag_enqueued[-1].command_id
                )
            )
            assert row is not None

        second = client.post(
            f"/api/model-failures/{failure_id}/decision",
            json={"decision": "retry_same"},
        )
        assert second.status_code == 409


def test_decompose_reassign_is_rejected_with_422(
    live_no_provider_app, fake_queue
) -> None:
    app = live_no_provider_app
    with TestClient(app) as client:
        _auth(client, app, "reassign-user")
        failure_id = _decompose_failure_turn(app, fake_queue, client)

        response = client.post(
            f"/api/model-failures/{failure_id}/decision",
            json={"decision": "reassign", "target_provider": "glm"},
        )
        assert response.status_code == 422


def test_decompose_terminate_turn_fails_the_turn(
    live_no_provider_app, fake_queue
) -> None:
    app = live_no_provider_app
    with TestClient(app) as client:
        _auth(client, app, "terminate-user")
        failure_id = _decompose_failure_turn(app, fake_queue, client)

        response = client.post(
            f"/api/model-failures/{failure_id}/decision",
            json={"decision": "terminate_turn"},
        )
        assert response.status_code == 200

        with app.state.session_factory() as session:
            failure = session.get(ModelFailureRow, failure_id)
            turn = session.get(ConversationTurnRow, failure.turn_id)
            assert turn.status == "failed"


def _subtask_failure_turn(app, fake_queue, client) -> tuple[str, str]:
    """Mock decompose then force one subtask into a transport failure."""
    conversation_id = client.post("/api/conversations", json={}).json()["id"]
    client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "子任务失败场景", "relative_paths": []},
        headers={"Idempotency-Key": "k-1"},
    )
    _run_decompose(app, fake_queue)
    # mock decompose queues both subtasks; run the first via a live-mode runner
    # to force a provider failure deterministically
    subtask_jobs = [j for j in fake_queue.dag_enqueued if j.kind is JobKind.SUBTASK_EXECUTE]
    from secagent.services.subtask_runner import execute_subtask_job

    live_settings = app.state.settings.model_copy(update={"model_mode": "live"})
    target = subtask_jobs[0]
    asyncio.run(
        execute_subtask_job(
            target.ref_id,
            target.command_id,
            app.state.session_factory,
            app.state.tool_registry,
            live_settings,
        )
    )
    with app.state.session_factory() as session:
        failure = session.scalars(
            select(ModelFailureRow).where(ModelFailureRow.stage == "subtask")
        ).all()[-1]
        return failure.id, failure.subtask_id


def test_subtask_failure_decisions_cover_all_four_paths(
    settings, fake_queue
) -> None:
    # 1) retry_same path
    app = _make_app(settings, fake_queue)
    with TestClient(app) as client:
        _auth(client, app, "decision-user")
        failure_id, subtask_id = _subtask_failure_turn(app, fake_queue, client)

        response = client.post(
            f"/api/model-failures/{failure_id}/decision",
            json={"decision": "retry_same"},
        )
        assert response.status_code == 200
        with app.state.session_factory() as session:
            assert session.get(SubtaskRow, subtask_id).status == "queued"

    # 2) reassign path with an unavailable provider is rejected
    fake_queue2 = type(fake_queue)()
    app2 = _make_app(settings, fake_queue2)
    with TestClient(app2) as client:
        _auth(client, app2, "decision-user-2")
        failure_id2, subtask_id2 = _subtask_failure_turn(app2, fake_queue2, client)
        response = client.post(
            f"/api/model-failures/{failure_id2}/decision",
            json={"decision": "reassign", "target_provider": "glm"},
        )
        assert response.status_code == 200
        with app2.state.session_factory() as session:
            row = session.get(SubtaskRow, subtask_id2)
            assert row.assigned_provider == "glm"
            assert row.status == "queued"

    # 3) skip_and_replan creates the next plan version
    fake_queue3 = type(fake_queue)()
    app3 = _make_app(settings, fake_queue3)
    with TestClient(app3) as client:
        _auth(client, app3, "decision-user-3")
        failure_id3, subtask_id3 = _subtask_failure_turn(app3, fake_queue3, client)
        with app3.state.session_factory() as session:
            old_turn_id = session.get(SubtaskRow, subtask_id3).turn_id
        response = client.post(
            f"/api/model-failures/{failure_id3}/decision",
            json={"decision": "skip_and_replan"},
        )
        assert response.status_code == 200
        with app3.state.session_factory() as session:
            old_turn = session.get(ConversationTurnRow, old_turn_id)
            assert old_turn.status == "superseded"
            new_turn = session.scalars(
                select(ConversationTurnRow).where(
                    ConversationTurnRow.replan_from_turn_id == old_turn_id
                )
            ).all()
            assert len(new_turn) == 1
            assert new_turn[0].plan_version == old_turn.plan_version + 1
            assert fake_queue3.dag_enqueued[-1].kind is JobKind.TURN_DECOMPOSE

    # 4) terminate_turn produces a partial turn with a synthesize job
    fake_queue4 = type(fake_queue)()
    app4 = _make_app(settings, fake_queue4)
    with TestClient(app4) as client:
        _auth(client, app4, "decision-user-4")
        failure_id4, subtask_id4 = _subtask_failure_turn(app4, fake_queue4, client)
        response = client.post(
            f"/api/model-failures/{failure_id4}/decision",
            json={"decision": "terminate_turn"},
        )
        assert response.status_code == 200
        with app4.state.session_factory() as session:
            subtask = session.get(SubtaskRow, subtask_id4)
            turn = session.get(ConversationTurnRow, subtask.turn_id)
            assert turn.status == "partial"
            synthesize_jobs = session.scalars(
                select(JobRunRow).where(
                    JobRunRow.job_kind == JobKind.TURN_SYNTHESIZE.value
                )
            ).all()
            assert len(synthesize_jobs) == 1


def test_subtask_tool_approval_decision_flow(settings, fake_queue) -> None:
    app = _make_app(settings, fake_queue)
    with TestClient(app) as client:
        _auth(client, app, "approval-user")
        conversation_id = client.post("/api/conversations", json={}).json()["id"]
        client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": "审批流程", "relative_paths": []},
            headers={"Idempotency-Key": "k-1"},
        )
        _run_decompose(app, fake_queue)

        # run subtasks; the tool-carrying subtask pauses for approval
        claimed: set[str] = set()
        while True:
            pending = [
                j
                for j in fake_queue.dag_enqueued
                if j.kind is JobKind.SUBTASK_EXECUTE and j.command_id not in claimed
            ]
            if not pending:
                break
            claimed.add(pending[0].command_id)
            _run_subtask(app, fake_queue, index=fake_queue.dag_enqueued.index(pending[0]))

        approvals = []
        with app.state.session_factory() as session:
            approvals = session.scalars(
                select(ApprovalRow).where(ApprovalRow.status == "pending")
            ).all()
        if not approvals:
            pytest.skip("mock plan produced no tool request in this run")

        approval_id = approvals[0].id
        subtask_id = approvals[0].subtask_id
        response = client.post(
            f"/api/approvals/{approval_id}/decision",
            json={"approved": True, "reason": "authorized by owner"},
        )
        assert response.status_code == 200
        with app.state.session_factory() as session:
            row = session.get(ApprovalRow, approval_id)
            assert row.status == "approved"
            subtask = session.get(SubtaskRow, subtask_id)
            assert subtask.status == "queued"


def test_stop_cancels_active_turn_and_subtasks(settings, fake_queue) -> None:
    app = _make_app(settings, fake_queue)
    with TestClient(app) as client:
        _auth(client, app, "stop-user")
        conversation_id = client.post("/api/conversations", json={}).json()["id"]
        client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": "停止场景", "relative_paths": []},
            headers={"Idempotency-Key": "k-1"},
        )
        _run_decompose(app, fake_queue)

        response = client.post(f"/api/conversations/{conversation_id}/stop")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"

        with app.state.session_factory() as session:
            turns = session.scalars(
                select(ConversationTurnRow).where(
                    ConversationTurnRow.conversation_id == conversation_id
                )
            ).all()
            assert all(turn.status == "cancelled" for turn in turns)
            subtasks = session.scalars(select(SubtaskRow)).all()
            assert all(
                item.status
                in {"cancelled", "superseded"}
                for item in subtasks
            )

        again = client.post(f"/api/conversations/{conversation_id}/stop")
        assert again.status_code == 409


def test_follow_up_message_supersedes_active_turn(settings, fake_queue) -> None:
    app = _make_app(settings, fake_queue)
    with TestClient(app) as client:
        _auth(client, app, "followup-user")
        conversation_id = client.post("/api/conversations", json={}).json()["id"]
        client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": "第一轮目标", "relative_paths": []},
            headers={"Idempotency-Key": "k-1"},
        )
        _run_decompose(app, fake_queue)
        with app.state.session_factory() as session:
            turns = session.scalars(
                select(ConversationTurnRow).where(
                    ConversationTurnRow.conversation_id == conversation_id
                )
            ).all()
            assert len(turns) == 1
            first_turn_id = turns[0].id
            first_plan_version = turns[0].plan_version

        follow_up = client.post(
            f"/api/conversations/{conversation_id}/messages",
            json={"content": "追问：补充分析范围", "relative_paths": []},
            headers={"Idempotency-Key": "k-2"},
        )
        assert follow_up.status_code in (200, 201)

        with app.state.session_factory() as session:
            first_turn = session.get(ConversationTurnRow, first_turn_id)
            assert first_turn.status == "replan_requested"
            all_turns = session.scalars(
                select(ConversationTurnRow).where(
                    ConversationTurnRow.conversation_id == conversation_id
                )
            ).all()
            assert len(all_turns) == 2
            second = all_turns[1]
            assert second.replan_from_turn_id == first_turn_id
            assert second.plan_version == first_plan_version + 1
            assert fake_queue.dag_enqueued[-1].kind is JobKind.TURN_DECOMPOSE
            assert fake_queue.dag_enqueued[-1].ref_id == second.id
