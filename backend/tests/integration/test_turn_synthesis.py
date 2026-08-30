"""End-to-end mock acceptance: one turn from message to streamed answer."""

import asyncio
import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from secagent.db_models import (
    ConversationEventRow,
    ConversationMessageRow,
    ConversationTurnRow,
    ModelCallRow,
    SubtaskRow,
)
from secagent.dag_domain import JobKind


def _run_decompose(app, fake_queue) -> None:
    from secagent.services.dag_orchestrator import execute_turn_decompose_job

    job = fake_queue.dag_enqueued[-1]
    assert job.kind is JobKind.TURN_DECOMPOSE
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


def _run_next_subtask(app, fake_queue, claimed: set[str]) -> bool:
    from secagent.services.subtask_runner import execute_subtask_job

    pending = [
        job
        for job in fake_queue.dag_enqueued
        if job.kind is JobKind.SUBTASK_EXECUTE
        and job.command_id not in claimed
    ]
    if not pending:
        return False
    claimed.add(pending[0].command_id)
    asyncio.run(
        execute_subtask_job(
            pending[0].ref_id,
            pending[0].command_id,
            app.state.session_factory,
            app.state.tool_registry,
            app.state.settings,
            queue=fake_queue,
        )
    )
    return True


def _run_synthesize(app, fake_queue) -> None:
    from secagent.services.dag_orchestrator import (
        execute_turn_synthesize_job,
    )

    jobs = [
        job
        for job in fake_queue.dag_enqueued
        if job.kind is JobKind.TURN_SYNTHESIZE
    ]
    assert jobs, "synthesize job was never created"
    asyncio.run(
        execute_turn_synthesize_job(
            jobs[0].ref_id,
            jobs[0].command_id,
            app.state.session_factory,
            app.state.tool_registry,
            app.state.settings,
        )
    )


def test_full_mock_turn_streams_the_final_answer(
    app, fake_queue, analyst_client
) -> None:
    conversation_id = analyst_client.post("/api/conversations", json={}).json()["id"]
    sent = analyst_client.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"content": "分析这个项目并找出高风险问题", "relative_paths": []},
        headers={"Idempotency-Key": "k-1"},
    )
    assert sent.status_code == 201
    turn_id = fake_queue.dag_enqueued[0].ref_id

    _run_decompose(app, fake_queue)

    claimed: set[str] = set()
    while _run_next_subtask(app, fake_queue, claimed):
        pass

    with app.state.session_factory() as session:
        subtasks = session.scalars(
            select(SubtaskRow).where(SubtaskRow.turn_id == turn_id)
        ).all()
        assert subtasks
        assert all(item.status == "completed" for item in subtasks)

    _run_synthesize(app, fake_queue)

    with app.state.session_factory() as session:
        turn = session.get(ConversationTurnRow, turn_id)
        assert turn.status == "completed"

        answer = session.scalars(
            select(ConversationMessageRow).where(
                ConversationMessageRow.conversation_id == conversation_id,
                ConversationMessageRow.kind == "assistant_answer",
            )
        ).all()
        assert len(answer) == 1
        assert answer[0].status == "completed"
        assert answer[0].turn_id == turn_id
        assert "演示结果" in answer[0].content

        events = session.scalars(
            select(ConversationEventRow)
            .where(ConversationEventRow.turn_id == turn_id)
            .order_by(ConversationEventRow.id.asc())
        ).all()
        types = [item.event_type for item in events]
        assert "turn.decomposition.started" in types
        assert "turn.synthesis.started" in types
        assert "turn.completed" in types
        deltas = [
            item
            for item in events
            if item.event_type == "assistant.answer.delta"
        ]
        assert deltas
        seqs = [json.loads(item.payload_json)["delta_seq"] for item in deltas]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        joined = "".join(json.loads(item.payload_json)["text"] for item in deltas)
        assert joined == answer[0].content
        completed_events = [
            item
            for item in events
            if item.event_type == "assistant.answer.completed"
        ]
        assert len(completed_events) == 1

        calls = session.scalars(
            select(ModelCallRow)
            .where(ModelCallRow.turn_id == turn_id)
            .order_by(ModelCallRow.created_at.asc(), ModelCallRow.id.asc())
        ).all()
        stages = [call.stage for call in calls]
        assert stages[0] == "decompose"
        assert "subtask_execute" in stages
        assert stages[-1] == "synthesize"
        assert all(call.is_demo for call in calls)
        assert all(call.provider == "mock" for call in calls)
