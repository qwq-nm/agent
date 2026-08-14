from sqlalchemy import select

from secagent.db_models import RefreshSessionRow, UserRow
from secagent.repository import AuthRepository
from secagent.services.auth_service import AuthService


def test_only_admin_can_list_and_create_users(admin_client, analyst_client):
    assert analyst_client.get("/api/admin/users").status_code == 403

    created = admin_client.post(
        "/api/admin/users",
        json={
            "username": "charlie",
            "password": "Charlie-Secure-9",
            "role": "analyst",
        },
    )

    assert created.status_code == 201
    assert created.json()["username"] == "charlie"
    assert created.json()["is_active"] is True
    assert "password" not in created.text
    assert {user["username"] for user in admin_client.get("/api/admin/users").json()} >= {
        "admin",
        "alice",
        "charlie",
    }


def test_duplicate_username_returns_conflict(admin_client, seeded_analyst):
    response = admin_client.post(
        "/api/admin/users",
        json={
            "username": "alice",
            "password": "Different-Secure-9",
            "role": "analyst",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "username_conflict"


def test_disabling_user_revokes_all_active_refresh_sessions_in_same_transaction(
    admin_client, analyst_client, app, seeded_analyst
):
    response = admin_client.patch(
        f"/api/admin/users/{seeded_analyst.id}", json={"is_active": False}
    )

    assert response.status_code == 200
    with app.state.session_factory() as session:
        user = session.get(UserRow, seeded_analyst.id)
        sessions = session.scalars(
            select(RefreshSessionRow).where(
                RefreshSessionRow.user_id == seeded_analyst.id
            )
        ).all()
        assert user.is_active is False
        assert sessions
        assert all(item.revoked_at is not None for item in sessions)

    assert analyst_client.post("/api/auth/refresh").status_code == 401


def test_replacing_password_revokes_refresh_and_changes_login(
    admin_client, analyst_client, app, seeded_analyst
):
    response = admin_client.patch(
        f"/api/admin/users/{seeded_analyst.id}",
        json={"password": "Replacement-Secure-9"},
    )

    assert response.status_code == 200
    with app.state.session_factory() as session:
        sessions = session.scalars(
            select(RefreshSessionRow).where(
                RefreshSessionRow.user_id == seeded_analyst.id
            )
        ).all()
        assert sessions
        assert all(item.revoked_at is not None for item in sessions)

    assert analyst_client.post("/api/auth/refresh").status_code == 401
    assert analyst_client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "Replacement-Secure-9"},
    ).status_code == 200


def test_cannot_disable_last_active_admin(admin_client, seeded_admin):
    response = admin_client.patch(
        f"/api/admin/users/{seeded_admin.id}", json={"is_active": False}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "last_active_admin"


def test_admin_patch_rejects_unsupported_or_empty_changes(admin_client, seeded_analyst):
    unsupported = admin_client.patch(
        f"/api/admin/users/{seeded_analyst.id}", json={"role": "admin"}
    )
    empty = admin_client.patch(f"/api/admin/users/{seeded_analyst.id}", json={})

    assert unsupported.status_code == 422
    assert empty.status_code == 422


def test_user_creation_enforces_ten_user_team_limit(admin_client):
    for index in range(1, 10):
        response = admin_client.post(
            "/api/admin/users",
            json={
                "username": f"user-{index}",
                "password": f"User-{index}-Secure-Pass",
                "role": "analyst",
            },
        )
        assert response.status_code == 201

    rejected = admin_client.post(
        "/api/admin/users",
        json={
            "username": "user-10",
            "password": "User-10-Secure-Pass",
            "role": "analyst",
        },
    )

    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "user_limit_reached"


def test_refresh_rotation_locks_user_to_serialize_admin_revocation(
    app, seeded_analyst
):
    with app.state.session_factory() as session:
        first_raw = AuthService.from_session(
            session, app.state.settings
        ).login("alice", "Correct-Horse-9").refresh_token

    events = []

    class LockTrackingRepository(AuthRepository):
        def get_user_for_update(self, user_id):
            events.append("user_lock")
            return super().get_user_for_update(user_id)

        def add_refresh_session(self, user_id, token_hash, expires_at):
            events.append("replacement")
            return super().add_refresh_session(user_id, token_hash, expires_at)

    with app.state.session_factory() as session:
        service = AuthService(
            session,
            app.state.settings,
            LockTrackingRepository(session),
        )
        service.refresh(first_raw)

    assert events == ["user_lock", "replacement"]
