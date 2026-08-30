import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import select

from secagent.config import Settings
from secagent.db_models import ConversationEventRow, ConversationTurnRow, JobRunRow
from secagent.dag_domain import JobKind
from secagent.main import create_app
from secagent.repositories.dag_repository import DagRepository
from secagent.services.dag_orchestrator import (
    execute_turn_decompose_job,
    republish_pending_dag_jobs,
)


def _send_message(client: TestClient, conversation_id: str, key: str = "k-1") -> dict:
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "分析这个日志并找出高风险问题", "relative_paths": []},
        headers={"Idempotency-Key": key},
    )
    assert response.status_code in (200, 201)
    return response.json()


def _run_decompose(app, fake_queue, index: int = -1) -> None:
    job = fake_queue.dag_enqueued[index]
    asyncio.run(
        execute_turn_decompose_job(
            job.ref_id,
            job.command_id,
            app.state.session_factory,
            app.state.tool_registry,
            app.state.settings,
        )
    )


def _turn(app, turn_id: str) -> ConversationTurnRow:
    with app.state.session_factory() as session:
        return session.get(ConversationTurnRow, turn_id)


def test_send_message_persists_a_durable_decompose_job(
    app, fake_queue, analyst_client
) -> None:
    created = analyst_client.post("/api/conversations", json={})
    assert created.status_code == 201
    conversation_id = created.json()["id"]

    _send_message(analyst_client, conversation_id)

    assert len(fake_queue.dag_enqueued) == 1
    job = fake_queue.dag_enqueued[0]
    assert job.kind is JobKind.TURN_DECOMPOSE

    with app.state.session_factory() as session:
        row = session.scalar(
            select(JobRunRow).where(JobRunRow.command_id == job.command_id)
        )
        assert row is not None
        assert row.status == "queued"
        assert row.job_kind == JobKind.TURN_DECOMPOSE.value
        assert row.turn_id == job.ref_id
        turn = session.get(ConversationTurnRow, job.ref_id)
        assert turn is not None and turn.status == "created"


def test_mock_decompose_job_persists_the_plan_and_model_call(
    app, fake_queue, analyst_client
) -> None:
    conversation_id = analyst_client.post("/api/conversations", json={}).json()["id"]
    _send_message(analyst_client, conversation_id)
    turn_id = fake_queue.dag_enqueued[0].ref_id
    command_id = fake_queue.dag_enqueued[0].command_id

    _run_decompose(app, fake_queue)

    with app.state.session_factory() as session:
        dag = DagRepository(session)
        turn = session.get(ConversationTurnRow, turn_id)
        assert turn.status == "scheduling"

        subtasks = dag.list_subtasks(turn_id)
        by_key = {item.key: item for item in subtasks}
        assert set(by_key) == {"extract_context", "assess_risk"}
        assert by_key["extract_context"].assigned_provider == "glm"
        assert by_key["assess_risk"].assigned_provider == "deepseek"
        assert all(item.status is not None for item in subtasks)

        events = session.scalars(
            select(ConversationEventRow)
            .where(ConversationEventRow.turn_id == turn_id)
            .order_by(ConversationEventRow.id.asc())
        ).all()
        types = [item.event_type for item in events]
        assert "turn.decomposition.started" in types
        assert "turn.plan.versioned" in types
        assert types.count("subtask.assigned") == 2
        assert types.index("turn.decomposition.started") < types.index(
            "turn.plan.versioned"
        )

        job = session.scalar(
            select(JobRunRow).where(JobRunRow.command_id == command_id)
        )
        assert job.status == "completed"


def test_decompose_records_model_call_linked_to_the_turn(
    app, fake_queue, analyst_client
) -> None:
    from secagent.db_models import ModelCallRow

    conversation_id = analyst_client.post("/api/conversations", json={}).json()["id"]
    _send_message(analyst_client, conversation_id)
    turn_id = fake_queue.dag_enqueued[0].ref_id

    _run_decompose(app, fake_queue)

    with app.state.session_factory() as session:
        calls = session.scalars(
            select(ModelCallRow).where(ModelCallRow.turn_id == turn_id)
        ).all()
        assert len(calls) == 1
        call = calls[0]
        assert call.stage == "decompose"
        assert call.provider == "mock"
        assert call.is_demo is True


def test_live_mode_without_deepseek_fails_safe(app, fake_queue) -> None:
    settings = Settings(
        database_url=app.state.settings.database_url,
        data_dir=app.state.settings.data_dir,
        model_mode="live",
        jwt_signing_key="test-signing-key-at-least-32-bytes",
    )
    live_app = create_app(settings, job_queue=fake_queue)

    with TestClient(live_app) as client:
        from secagent.services.auth_service import AuthService
        from secagent.domain import UserRole

        with live_app.state.session_factory() as session:
            AuthService.from_session(session, settings).create_user(
                "live-user", "Live-Pass-9", UserRole.ANALYST
            )
        login = client.post(
            "/api/auth/login", json={"username": "live-user", "password": "Live-Pass-9"}
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        conversation_id = client.post("/api/conversations", json={}).json()["id"]
        _send_message(client, conversation_id)
        turn_id = fake_queue.dag_enqueued[-1].ref_id

    _run_decompose(live_app, fake_queue)

    with live_app.state.session_factory() as session:
        turn = session.get(ConversationTurnRow, turn_id)
        assert turn.status == "waiting_model_decision"
        from secagent.db_models import ModelFailureRow, SubtaskRow

        failure = session.scalar(
            select(ModelFailureRow).where(ModelFailureRow.turn_id == turn_id)
        )
        assert failure is not None
        assert failure.stage == "decompose"
        assert failure.status == "waiting_decision"
        assert failure.decision is None

        subtasks = session.scalars(
            select(SubtaskRow).where(SubtaskRow.turn_id == turn_id)
        ).all()
        assert subtasks == []


def test_enqueue_failure_is_recovered_by_startup_republish(
    app, fake_queue, analyst_client
) -> None:
    conversation_id = analyst_client.post("/api/conversations", json={}).json()["id"]
    fake_queue.fail_next_enqueue = True
    _send_message(analyst_client, conversation_id)
    turn_id = None
    with app.state.session_factory() as session:
        row = session.scalars(
            select(JobRunRow).where(JobRunRow.job_kind == JobKind.TURN_DECOMPOSE.value)
        ).all()[-1]
        assert row.status == "enqueue_failed"
        turn_id = row.turn_id

    published = republish_pending_dag_jobs(app.state.session_factory, fake_queue)

    assert published == 1
    assert fake_queue.dag_enqueued[-1].ref_id == turn_id
    with app.state.session_factory() as session:
        row = session.scalars(
            select(JobRunRow).where(JobRunRow.job_kind == JobKind.TURN_DECOMPOSE.value)
        ).all()[-1]
        assert row.status == "queued"

    # A recovered job must actually be executable: the compat task is back to
    # queued so the legacy claim path does not cancel the job.
    job = fake_queue.dag_enqueued[-1]
    asyncio.run(
        execute_turn_decompose_job(
            job.ref_id,
            job.command_id,
            app.state.session_factory,
            app.state.tool_registry,
            app.state.settings,
        )
    )
    with app.state.session_factory() as session:
        turn = session.get(ConversationTurnRow, turn_id)
        assert turn.status == "scheduling"


def test_idempotent_replay_does_not_duplicate_the_job(
    app, fake_queue, analyst_client
) -> None:
    conversation_id = analyst_client.post("/api/conversations", json={}).json()["id"]
    _send_message(analyst_client, conversation_id, key="same-key")
    _send_message(analyst_client, conversation_id, key="same-key")

    assert len(fake_queue.dag_enqueued) == 1
