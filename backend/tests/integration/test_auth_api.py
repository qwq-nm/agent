from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

import jwt
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import select

from secagent.auth.dependencies import AuthenticatedUser, current_user, require_admin
from secagent.cli import main as cli_main
from secagent.db_models import RefreshSessionRow, UserRow
from secagent.repository import AuthRepository
from secagent.services.auth_service import AuthenticationError, AuthService


def test_login_sets_refresh_cookie(client, seeded_analyst):
    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "Correct-Horse-9"},
    )

    assert response.status_code == 200
    assert set(response.json()) == {"access_token", "token_type", "expires_in", "user"}
    assert response.json()["token_type"] == "bearer"
    assert response.json()["expires_in"] == 900
    assert response.json()["user"] == {
        "id": seeded_analyst.id,
        "username": "alice",
        "role": "analyst",
    }
    assert response.json()["access_token"]
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/api/auth" in cookie
    assert "Max-Age=604800" in cookie


def test_login_stores_only_refresh_hash(client, app, seeded_analyst):
    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "Correct-Horse-9"},
    )
    raw = response.cookies["refresh_token"]

    with app.state.session_factory() as session:
        stored = session.scalar(select(RefreshSessionRow))
        assert stored is not None
        assert stored.token_hash != raw
        assert raw not in stored.token_hash


def test_login_rejects_invalid_password_and_disabled_user(client, app, seeded_analyst):
    invalid = client.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong"}
    )

    with app.state.session_factory() as session:
        row = session.get(UserRow, seeded_analyst.id)
        row.is_active = False
        session.commit()
    disabled = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "Correct-Horse-9"},
    )

    assert invalid.status_code == 401
    assert disabled.status_code == 401
    assert invalid.json() == disabled.json()


def test_missing_signing_key_does_not_persist_refresh_session(
    client, app, seeded_analyst
):
    app.state.settings.jwt_signing_key = None

    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "Correct-Horse-9"},
    )

    assert response.status_code == 503
    with app.state.session_factory() as session:
        assert session.scalar(select(RefreshSessionRow)) is None


def test_unreadable_signing_key_file_returns_generic_503(
    app, seeded_analyst, tmp_path
):
    missing_file = tmp_path / "private" / "signing-key"
    app.state.settings.jwt_signing_key_file = missing_file

    with TestClient(app, raise_server_exceptions=False) as non_raising_client:
        response = non_raising_client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "Correct-Horse-9"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Authentication unavailable"}
    assert str(missing_file) not in response.text
    with app.state.session_factory() as session:
        assert session.scalar(select(RefreshSessionRow)) is None


def test_refresh_rotates_and_replay_is_rejected(client, app, seeded_analyst):
    login = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "Correct-Horse-9"},
    )
    first_raw = login.cookies["refresh_token"]

    refreshed = client.post("/api/auth/refresh")
    second_raw = refreshed.cookies["refresh_token"]

    assert refreshed.status_code == 200
    assert second_raw != first_raw
    with app.state.session_factory() as session:
        rows = session.scalars(
            select(RefreshSessionRow).order_by(RefreshSessionRow.created_at)
        ).all()
        assert len(rows) == 2
        assert rows[0].revoked_at is not None
        assert rows[0].replaced_by_id == rows[1].id
        assert rows[1].revoked_at is None

    client.cookies.set("refresh_token", first_raw, path="/api/auth")
    replay = client.post("/api/auth/refresh")
    assert replay.status_code == 401


def test_logout_revokes_refresh_session_and_clears_cookie(client, app, seeded_analyst):
    client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "Correct-Horse-9"},
    )

    response = client.post("/api/auth/logout")

    assert response.status_code == 204
    assert response.headers["set-cookie"].startswith("refresh_token=")
    assert "Max-Age=0" in response.headers["set-cookie"]
    with app.state.session_factory() as session:
        stored = session.scalar(select(RefreshSessionRow))
        assert stored.revoked_at is not None


def test_logout_with_rotated_token_revokes_its_current_successor(
    client, app, seeded_analyst
):
    login = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "Correct-Horse-9"},
    )
    first_raw = login.cookies["refresh_token"]
    refreshed = client.post("/api/auth/refresh")
    second_raw = refreshed.cookies["refresh_token"]

    client.cookies.clear()
    client.cookies.set("refresh_token", first_raw, path="/api/auth")
    assert client.post("/api/auth/logout").status_code == 204

    client.cookies.set("refresh_token", second_raw, path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 401
    with app.state.session_factory() as session:
        rows = session.scalars(select(RefreshSessionRow)).all()
        assert len(rows) == 2
        assert all(row.revoked_at is not None for row in rows)


def test_logout_linearizes_after_an_in_flight_rotation(app, seeded_analyst):
    with app.state.session_factory() as session:
        first_raw = AuthService.from_session(
            session, app.state.settings
        ).login("alice", "Correct-Horse-9").refresh_token

    rotation_claimed = Event()
    allow_rotation_to_commit = Event()
    logout_entered_repository = Event()

    class PausedRotationRepository(AuthRepository):
        def add_refresh_session(self, user_id, token_hash, expires_at):
            rotation_claimed.set()
            assert allow_rotation_to_commit.wait(timeout=5)
            return super().add_refresh_session(user_id, token_hash, expires_at)

    class AnnouncedLogoutRepository(AuthRepository):
        def revoke_refresh_lineage(self, token_hash, now):
            logout_entered_repository.set()
            return super().revoke_refresh_lineage(token_hash, now)

    def rotate():
        with app.state.session_factory() as session:
            service = AuthService(
                session,
                app.state.settings,
                PausedRotationRepository(session),
            )
            return service.refresh(first_raw).refresh_token

    def logout():
        with app.state.session_factory() as session:
            service = AuthService(
                session,
                app.state.settings,
                AnnouncedLogoutRepository(session),
            )
            service.logout(first_raw)

    with ThreadPoolExecutor(max_workers=2) as executor:
        rotation_future = executor.submit(rotate)
        assert rotation_claimed.wait(timeout=5)
        logout_future = executor.submit(logout)
        assert logout_entered_repository.wait(timeout=5)
        allow_rotation_to_commit.set()
        second_raw = rotation_future.result(timeout=5)
        logout_future.result(timeout=5)

    with app.state.session_factory() as session:
        service = AuthService.from_session(session, app.state.settings)
        with pytest.raises(AuthenticationError):
            service.refresh(second_raw)


def test_current_user_and_require_admin_dependencies(app, admin_client, analyst_client):
    @app.get("/test/current-user")
    def read_actor(actor: AuthenticatedUser = Depends(current_user)):
        return actor

    @app.get("/test/admin")
    def read_admin(actor: AuthenticatedUser = Depends(require_admin)):
        return actor

    assert analyst_client.get("/test/current-user").json()["username"] == "alice"
    assert analyst_client.get("/test/admin").status_code == 403
    assert admin_client.get("/test/admin").status_code == 200


def test_current_user_rejects_expired_access_token(app, client, seeded_analyst):
    @app.get("/test/expired")
    def read_actor(actor: AuthenticatedUser = Depends(current_user)):
        return actor

    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": seeded_analyst.id,
            "role": "analyst",
            "iat": now - timedelta(minutes=2),
            "exp": now - timedelta(minutes=1),
            "jti": "expired-jti",
        },
        app.state.settings.jwt_key(),
        algorithm="HS256",
    )

    response = client.get("/test/expired", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_create_admin_cli_prompts_for_password(app):
    prompts = iter(["Interactive-Pass-9", "Interactive-Pass-9"])

    result = cli_main(
        ["create-admin", "--username", "bootstrap"],
        settings=app.state.settings,
        password_reader=lambda _prompt: next(prompts),
    )

    assert result == 0
    with app.state.session_factory() as session:
        row = session.scalar(select(UserRow).where(UserRow.username == "bootstrap"))
        assert row is not None
        assert row.role == "admin"
        assert row.password_hash != "Interactive-Pass-9"
