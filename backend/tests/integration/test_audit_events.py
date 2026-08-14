import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from sqlalchemy import select

from secagent.db_models import ApprovalRow, AuditEventRow, TaskStepRow
from secagent.domain import PlanStep, RiskLevel, TaskStatus
from secagent.services.audit import AuditService


def _events(app, action):
    with app.state.session_factory() as session:
        return list(
            session.scalars(
                select(AuditEventRow)
                .where(AuditEventRow.action == action)
                .order_by(AuditEventRow.id)
            ).all()
        )


def test_forbidden_access_is_audited(alice_client, bob_task, app, seeded_analyst):
    alice_client.get(f"/api/tasks/{bob_task['id']}")

    event = _events(app, "task.access_denied")[-1]
    assert event.actor_id == seeded_analyst.id
    assert event.resource_id == bob_task["id"]
    assert event.outcome == "denied"


def test_login_failure_is_audited_without_credentials(client, app, seeded_analyst):
    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "Never-Audit-This-Password"},
    )

    assert response.status_code == 401
    event = _events(app, "auth.login_failed")[-1]
    assert event.actor_id is None
    assert event.resource_id != "alice"
    assert "Never-Audit-This-Password" not in event.details_json


def test_login_failure_hashes_attacker_controlled_identifier(client, app):
    secret_username = "sk-attacker-supplied-api-key"

    response = client.post(
        "/api/auth/login",
        json={"username": secret_username, "password": "wrong-password"},
    )

    assert response.status_code == 401
    event = _events(app, "auth.login_failed")[-1]
    assert secret_username not in event.resource_id
    assert secret_username not in event.details_json


def test_audit_service_recursively_redacts_sensitive_fields(app, seeded_admin):
    class SecretObject:
        def __str__(self):
            return "object-secret-must-not-be-serialized"

    with app.state.session_factory() as session:
        AuditService(session).record(
            seeded_admin.id,
            "test.redaction",
            "test",
            "one",
            "success",
            {
                "password": "plain-password",
                "nested": {
                    "jwt": "header.payload.signature",
                    "items": [{"apiKey": "api-secret"}, {"safe": "ok"}],
                },
                "cookie_value": "session-cookie",
                "external_response": {"body": "provider secret body"},
                "stack_trace": "sensitive stack",
                "sequence": ("eyJhbGciOiJIUzI1NiJ9.payload.signature",),
                "object": SecretObject(),
            },
        )
        session.commit()

    details = json.loads(_events(app, "test.redaction")[-1].details_json)
    assert details["password"] == "***REDACTED***"
    assert details["nested"]["jwt"] == "***REDACTED***"
    assert details["nested"]["items"][0]["apiKey"] == "***REDACTED***"
    assert details["nested"]["items"][1]["safe"] == "ok"
    assert details["external_response"] == "***REDACTED***"
    assert details["stack_trace"] == "***REDACTED***"
    assert "plain-password" not in json.dumps(details)
    assert "provider secret body" not in json.dumps(details)
    assert "eyJhbGciOiJIUzI1NiJ9.payload.signature" not in json.dumps(details)
    assert "object-secret-must-not-be-serialized" not in json.dumps(details)


def test_audit_service_redacts_secret_shaped_resource_identifiers(app, seeded_admin):
    with app.state.session_factory() as session:
        AuditService(session).record(
            seeded_admin.id,
            "test.resource_redaction",
            "test",
            "sk-attacker-supplied-api-key",
            "failure",
            {},
        )
        session.commit()

    event = _events(app, "test.resource_redaction")[-1]
    assert "sk-attacker-supplied-api-key" not in event.resource_id
    assert "REDACTED" in event.resource_id


def test_provider_check_and_task_status_actions_are_audited(
    analyst_client, app
):
    created = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Inspect action audit",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    analyst_client.post(f"/api/tasks/{created['id']}/pause")
    assert analyst_client.get("/api/models/status").status_code == 200

    assert _events(app, "task.create")[-1].resource_id == created["id"]
    assert _events(app, "task.pause")[-1].outcome == "success"
    assert _events(app, "provider.check")[-1].outcome == "success"


@pytest.mark.parametrize("legacy_null_expiry", [False, True])
def test_expired_approval_returns_conflict_and_keeps_task_waiting(
    analyst_client, app, seeded_analyst, legacy_null_expiry
):
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Inspect an approval expiry",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    with app.state.session_factory() as session:
        from secagent.repository import TaskRepository

        repository = TaskRepository(session)
        repository.set_task_status(task["id"], TaskStatus.RUNNING)
        step_id = repository.add_step(
            task["id"],
            0,
            PlanStep(
                name="Fetch approved URL",
                purpose="Passive inspection",
                tool_name="http_fetch",
                params={},
                risk_level=RiskLevel.MEDIUM,
                need_human_confirm=True,
            ),
        )
        approval_id = repository.add_approval(
            task["id"],
            step_id=step_id,
            tool_name="http_fetch",
            risk_level="medium",
            params_summary="{}",
        )
        repository.set_task_status(task["id"], TaskStatus.WAITING_HUMAN)
        approval = session.get(ApprovalRow, approval_id)
        if legacy_null_expiry:
            approval.expires_at = None
            approval.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
        else:
            approval.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()

    response = analyst_client.post(
        f"/api/tasks/{task['id']}/approve",
        json={"approved": True, "reason": "approved too late"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval_expired"
    with app.state.session_factory() as session:
        assert session.get(ApprovalRow, approval_id).status == "expired"
        assert session.get(TaskStepRow, step_id).status == "pending"
    detail = analyst_client.get(f"/api/tasks/{task['id']}").json()
    assert detail["status"] == "waiting_human"
    event = _events(app, "approval.expired")[-1]
    assert event.actor_id == seeded_analyst.id
    assert event.resource_id == approval_id


def test_missing_pending_approval_is_state_conflict_and_is_audited(
    analyst_client, repository, app
):
    task = analyst_client.post(
        "/api/tasks",
        json={
            "goal": "Inspect missing approval state",
            "authorization_scope": "Uploaded logs only",
        },
    ).json()
    repository.set_task_status(task["id"], TaskStatus.RUNNING)
    repository.set_task_status(task["id"], TaskStatus.WAITING_HUMAN)

    response = analyst_client.post(
        f"/api/tasks/{task['id']}/approve",
        json={"approved": True, "reason": "no pending approval exists"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    event = _events(app, "task.approve")[-1]
    assert event.resource_id == task["id"]
    assert event.outcome == "failure"


def test_only_one_concurrent_approval_decision_can_consume_pending_row(app):
    with app.state.session_factory() as session:
        from secagent.domain import TaskCreate
        from secagent.repository import TaskRepository

        repository = TaskRepository(session)
        task = repository.create_task(
            TaskCreate(
                goal="Inspect concurrent approval",
                authorization_scope="Uploaded logs only",
            )
        )
        step_id = repository.add_step(
            task.id,
            0,
            PlanStep(
                name="Concurrent decision",
                purpose="Verify single consumption",
                tool_name="http_fetch",
                params={},
                risk_level=RiskLevel.MEDIUM,
                need_human_confirm=True,
            ),
        )
        approval_id = repository.add_approval(
            task.id,
            step_id=step_id,
            tool_name="http_fetch",
            risk_level="medium",
            params_summary="{}",
        )

    start = Barrier(2)

    def decide(approved):
        start.wait(timeout=5)
        with app.state.session_factory() as session:
            from secagent.repository import TaskRepository

            try:
                TaskRepository(session).decide_latest_approval(
                    task.id,
                    approved=approved,
                    reason="concurrent decision",
                )
            except ValueError:
                session.rollback()
                return "conflict"
            return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            executor.submit(decide, True),
            executor.submit(decide, False),
        ]
        outcomes = sorted(result.result(timeout=10) for result in results)

    assert outcomes == ["conflict", "success"]
    with app.state.session_factory() as session:
        approval = session.get(ApprovalRow, approval_id)
        assert approval.status in {"approved", "rejected"}


def test_only_one_concurrent_transition_can_leave_waiting_human(app):
    with app.state.session_factory() as session:
        from secagent.domain import TaskCreate
        from secagent.repository import TaskRepository

        repository = TaskRepository(session)
        task = repository.create_task(
            TaskCreate(
                goal="Inspect concurrent transition",
                authorization_scope="Uploaded logs only",
            )
        )
        repository.set_task_status(task.id, TaskStatus.RUNNING)
        repository.set_task_status(task.id, TaskStatus.WAITING_HUMAN)

    start = Barrier(2)

    def transition(target):
        start.wait(timeout=5)
        with app.state.session_factory() as session:
            from secagent.repository import TaskRepository

            try:
                TaskRepository(session).transition_task_status(
                    task.id, TaskStatus.WAITING_HUMAN, target
                )
            except ValueError:
                session.rollback()
                return "conflict"
            return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            executor.submit(transition, TaskStatus.RUNNING),
            executor.submit(transition, TaskStatus.CANCELLED),
        ]
        outcomes = sorted(result.result(timeout=10) for result in results)

    assert outcomes == ["conflict", "success"]


def test_admin_can_list_audit_events(admin_client, analyst_client):
    analyst_client.get("/api/models/status")

    response = admin_client.get("/api/admin/audit-events")

    assert response.status_code == 200
    assert response.json()
    assert {"id", "actor_id", "action", "resource_type", "resource_id", "outcome", "details", "created_at"} <= set(response.json()[0])
