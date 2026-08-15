from cryptography.fernet import Fernet
from sqlalchemy import select

from secagent.db_models import AuditEventRow


def _enable_credential_storage(app) -> None:
    app.state.settings.provider_credential_encryption_key = Fernet.generate_key().decode()


def _events(app, action: str) -> list[AuditEventRow]:
    with app.state.session_factory() as session:
        return list(
            session.scalars(
                select(AuditEventRow)
                .where(AuditEventRow.action == action)
                .order_by(AuditEventRow.id)
            ).all()
        )


def test_admin_can_save_replace_list_and_clear_provider_credentials(
    admin_client, app, seeded_admin
):
    _enable_credential_storage(app)
    fake_key = "test-provider-key-1234"

    saved = admin_client.put(
        "/api/admin/provider-credentials/deepseek", json={"api_key": fake_key}
    )
    assert saved.status_code == 200
    assert saved.json() == {
        "provider": "deepseek",
        "configured": True,
        "key_hint": "...1234",
        "updated_at": saved.json()["updated_at"],
    }
    assert fake_key not in saved.text

    replaced = admin_client.put(
        "/api/admin/provider-credentials/deepseek",
        json={"api_key": "test-replacement-5678"},
    )
    assert replaced.status_code == 200
    assert replaced.json()["key_hint"] == "...5678"
    assert fake_key not in replaced.text

    listed = admin_client.get("/api/admin/provider-credentials")
    assert listed.status_code == 200
    assert listed.json()[0] == {
        "provider": "deepseek",
        "configured": True,
        "key_hint": "...5678",
        "updated_at": listed.json()[0]["updated_at"],
    }
    assert listed.json()[0]["updated_at"] is not None
    assert listed.json()[1] == {
        "provider": "glm",
        "configured": False,
        "key_hint": None,
        "updated_at": None,
    }
    assert fake_key not in listed.text

    cleared = admin_client.delete("/api/admin/provider-credentials/deepseek")
    assert cleared.status_code == 200
    assert cleared.json()["configured"] is False
    assert cleared.json()["key_hint"] is None
    assert fake_key not in cleared.text

    listed_after_clear = admin_client.get("/api/admin/provider-credentials")
    assert listed_after_clear.status_code == 200
    assert listed_after_clear.json()[0]["configured"] is False
    assert fake_key not in listed_after_clear.text

    saved_event = _events(app, "provider.credentials.save")[-1]
    cleared_event = _events(app, "provider.credentials.clear")[-1]
    assert saved_event.actor_id == seeded_admin.id
    assert saved_event.details_json == '{"provider": "deepseek"}'
    assert cleared_event.details_json == '{"provider": "deepseek"}'
    assert fake_key not in saved_event.details_json
    assert fake_key not in cleared_event.details_json


def test_provider_credentials_are_admin_only(admin_client, analyst_client, app):
    _enable_credential_storage(app)

    assert analyst_client.get("/api/admin/provider-credentials").status_code == 403
    assert (
        analyst_client.put(
            "/api/admin/provider-credentials/deepseek",
            json={"api_key": "test-provider-key-1234"},
        ).status_code
        == 403
    )
    assert admin_client.get("/api/admin/provider-credentials").status_code == 200


def test_provider_credential_write_validates_body_and_provider(admin_client, app):
    _enable_credential_storage(app)

    for payload in (
        {"api_key": "   "},
        {"api_key": "test-provider\nkey"},
        {"api_key": "x" * 513},
        {"api_key": "test-provider-key-1234", "unexpected": "value"},
    ):
        response = admin_client.put(
            "/api/admin/provider-credentials/deepseek", json=payload
        )
        assert response.status_code == 422
        assert "test-provider-key-1234" not in response.text

    unsupported = admin_client.put(
        "/api/admin/provider-credentials/other",
        json={"api_key": "test-provider-key-1234"},
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "validation_error"


def test_credential_storage_configuration_errors_are_unavailable(admin_client):
    response = admin_client.put(
        "/api/admin/provider-credentials/deepseek",
        json={"api_key": "test-provider-key-1234"},
    )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "credential_storage_unavailable",
        "message": "Provider credential storage is unavailable",
        "fields": None,
        "trace_id": response.json()["error"]["trace_id"],
    }
    assert "test-provider-key-1234" not in response.text
