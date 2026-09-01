"""Chitchat fast path and replan-race recovery for conversation turns.

Regression coverage for two related production issues:

1. A plain greeting like "hi" used to run the full DeepSeek decomposition
   pipeline (100+ seconds of model time, real subtasks, synthesis). The fast
   path now completes the turn with a static reply and no subtasks.
2. When a follow-up message superseded a turn while its decompose job was
   still running, the stale job crashed on the illegal transition, stayed
   ``running`` forever, and was re-queued as a zombie on every restart. The
   orchestrator and recovery paths now discard such stale jobs cleanly.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from secagent.agents.coordinator import CoordinatorAgent
from secagent.conversation_domain import ConversationMessageKind
from secagent.dag_domain import JobKind
from secagent.db_models import (
    ConversationEventRow,
    ConversationMessageRow,
    ConversationTurnRow,
    JobRunRow,
    SubtaskRow,
)
from secagent.repositories.dag_repository import DagRepository
from secagent.services.dag_orchestrator import (
    execute_turn_decompose_job,
    republish_pending_dag_jobs,
)


def _send(
    client,
    conversation_id: str,
    content: str,
    key: str,
) -> dict:
    response = client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": content, "relative_paths": []},
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


def _job_row(app, command_id: str) -> JobRunRow:
    with app.state.session_factory() as session:
        return session.scalar(
            select(JobRunRow).where(JobRunRow.command_id == command_id)
        )


def _turn(app, turn_id: str) -> ConversationTurnRow:
    with app.state.session_factory() as session:
        return session.get(ConversationTurnRow, turn_id)


def test_greeting_completes_turn_without_subtasks_or_extra_jobs(
    app, fake_queue, analyst_client
) -> None:
    conversation_id = analyst_client.post("/api/conversations", json={}).json()["id"]
    _send(analyst_client, conversation_id, "hi", key="greet-1")

    assert len(fake_queue.dag_enqueued) == 1
    turn_id = fake_queue.dag_enqueued[0].ref_id
    command_id = fake_queue.dag_enqueued[0].command_id

    _run_decompose(app, fake_queue)

    assert _turn(app, turn_id).status == "completed"
    with app.state.session_factory() as session:
        subtasks = session.scalars(
            select(SubtaskRow).where(SubtaskRow.turn_id == turn_id)
        ).all()
        assert subtasks == []
        answers = session.scalars(
            select(ConversationMessageRow).where(
                ConversationMessageRow.turn_id == turn_id,
                ConversationMessageRow.kind
                == ConversationMessageKind.ASSISTANT_ANSWER.value,
            )
        ).all()
        assert len(answers) == 1
        assert "SecAgent-X" in answers[0].content
        events = session.scalars(
            select(ConversationEventRow)
            .where(ConversationEventRow.turn_id == turn_id)
            .order_by(ConversationEventRow.id.asc())
        ).all()
        types = [item.event_type for item in events]
        assert "assistant.answer.completed" in types
        assert "turn.completed" in types
    assert _job_row(app, command_id).status == "completed"
    # Only the original decompose job ever existed; no subtask/synthesize jobs.
    assert len(fake_queue.dag_enqueued) == 1


def test_short_greeting_with_intent_keyword_runs_full_pipeline(
    app, fake_queue, analyst_client
) -> None:
    conversation_id = analyst_client.post("/api/conversations", json={}).json()["id"]
    _send(analyst_client, conversation_id, "hi 分析一下这个日志", key="intent-1")

    turn_id = fake_queue.dag_enqueued[0].ref_id
    _run_decompose(app, fake_queue)

    assert _turn(app, turn_id).status == "scheduling"
    with app.state.session_factory() as session:
        subtasks = session.scalars(
            select(SubtaskRow).where(SubtaskRow.turn_id == turn_id)
        ).all()
        assert subtasks  # intent messages still decompose into real subtasks


def test_superseded_before_decompose_discards_without_crashing(
    app, fake_queue, analyst_client
) -> None:
    conversation_id = analyst_client.post("/api/conversations", json={}).json()["id"]
    _send(analyst_client, conversation_id, "分析这个日志", key="first")
    first_job = fake_queue.dag_enqueued[0]
    v1 = first_job.ref_id

    # The worker claimed the job and moved the turn into decomposing before the
    # follow-up message arrived.
    with app.state.session_factory() as session:
        DagRepository(session).mark_turn_state(v1, "decomposing", expected={"created"})
        session.commit()

    _send(analyst_client, conversation_id, "再分析一遍", key="second")
    assert len(fake_queue.dag_enqueued) == 2

    # The stale v1 decompose job must not crash and must land in a final state.
    asyncio.run(
        execute_turn_decompose_job(
            first_job.ref_id,
            first_job.command_id,
            app.state.session_factory,
            app.state.tool_registry,
            app.state.settings,
        )
    )
    assert _turn(app, v1).status == "replan_requested"
    assert _job_row(app, first_job.command_id).status == "completed"
    # No subtasks were created for the discarded turn.
    with app.state.session_factory() as session:
        subtasks = session.scalars(
            select(SubtaskRow).where(SubtaskRow.turn_id == v1)
        ).all()
        assert subtasks == []


def test_superseded_during_model_call_discards_stale_plan(
    app, fake_queue, analyst_client, monkeypatch
) -> None:
    conversation_id = analyst_client.post("/api/conversations", json={}).json()["id"]
    _send(analyst_client, conversation_id, "分析这个日志", key="first")
    first_job = fake_queue.dag_enqueued[0]
    v1 = first_job.ref_id

    original_decompose = CoordinatorAgent.decompose

    async def superseding_decompose(self, context, *, plan_version, registered_tools, authorized_tools):
        # Simulate the follow-up message arriving while the model call is in
        # flight: the turn moves to replan_requested before the model returns.
        with app.state.session_factory() as session:
            DagRepository(session).mark_turn_state(
                v1, "replan_requested", expected={"decomposing"}
            )
            session.commit()
        return await original_decompose(
            self,
            context,
            plan_version=plan_version,
            registered_tools=registered_tools,
            authorized_tools=authorized_tools,
        )

    monkeypatch.setattr(CoordinatorAgent, "decompose", superseding_decompose)

    asyncio.run(
        execute_turn_decompose_job(
            first_job.ref_id,
            first_job.command_id,
            app.state.session_factory,
            app.state.tool_registry,
            app.state.settings,
        )
    )
    assert _turn(app, v1).status == "replan_requested"
    assert _job_row(app, first_job.command_id).status in {"completed", "failed"}
    with app.state.session_factory() as session:
        subtasks = session.scalars(
            select(SubtaskRow).where(SubtaskRow.turn_id == v1)
        ).all()
        assert subtasks == []


def test_recover_expired_marks_superseded_turn_job_failed(
    app, fake_queue, analyst_client
) -> None:
    conversation_id = analyst_client.post("/api/conversations", json={}).json()["id"]
    _send(analyst_client, conversation_id, "分析这个日志", key="first")
    first_job = fake_queue.dag_enqueued[0]
    v1 = first_job.ref_id

    with app.state.session_factory() as session:
        DagRepository(session).mark_turn_state(v1, "decomposing", expected={"created"})
        job = session.scalar(
            select(JobRunRow).where(JobRunRow.command_id == first_job.command_id)
        )
        job.status = "running"
        job.worker_id = "worker-1"
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    with app.state.session_factory() as session:
        DagRepository(session).mark_turn_state(
            v1, "replan_requested", expected={"decomposing"}
        )
        session.commit()

    with app.state.session_factory() as session:
        recovered = DagRepository(session).recover_expired_dag_jobs()
    assert recovered == 1
    job = _job_row(app, first_job.command_id)
    assert job.status == "failed"  # never re-queued as a zombie
    assert _turn(app, v1).status == "replan_requested"


def test_republish_skips_superseded_turn_job(
    app, fake_queue, analyst_client
) -> None:
    conversation_id = analyst_client.post("/api/conversations", json={}).json()["id"]
    _send(analyst_client, conversation_id, "分析这个日志", key="first")
    first_job = fake_queue.dag_enqueued[0]
    v1 = first_job.ref_id

    with app.state.session_factory() as session:
        job = session.scalar(
            select(JobRunRow).where(JobRunRow.command_id == first_job.command_id)
        )
        job.status = "pending_publish"
        session.commit()
    with app.state.session_factory() as session:
        DagRepository(session).mark_turn_state(
            v1, "superseded", expected={"created"}
        )
        session.commit()

    published = republish_pending_dag_jobs(app.state.session_factory, fake_queue)
    assert published == 0
    assert _job_row(app, first_job.command_id).status == "failed"
    assert _turn(app, v1).status == "superseded"
