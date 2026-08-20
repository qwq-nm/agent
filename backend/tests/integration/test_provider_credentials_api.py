from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select

from secagent.db_models import AuditEventRow, ProviderCredentialRow


TEST_API_KEY = "test-provider-key-1234"


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


def _assert_storage_unavailable(response) -> None:
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "credential_storage_unavailable",
        "message": "Provider credential storage is unavailable",
        "fields": None,
        "trace_id": response.json()["error"]["trace_id"],
    }


def test_admin_can_save_replace_list_and_clear_provider_credentials(
    admin_client, app, seeded_admin
):
    _enable_credential_storage(app)
    fake_key = TEST_API_KEY

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
            json={"api_key": TEST_API_KEY},
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
        {"api_key": TEST_API_KEY, "unexpected": "value"},
    ):
        response = admin_client.put(
            "/api/admin/provider-credentials/deepseek", json=payload
        )
        assert response.status_code == 422
        assert TEST_API_KEY not in response.text

    unsupported = admin_client.put(
        "/api/admin/provider-credentials/other",
        json={"api_key": TEST_API_KEY},
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "validation_error"


def test_credential_storage_configuration_errors_are_unavailable(admin_client):
    response = admin_client.put(
        "/api/admin/provider-credentials/deepseek",
        json={"api_key": TEST_API_KEY},
    )

    _assert_storage_unavailable(response)
    assert TEST_API_KEY not in response.text


@pytest.mark.parametrize("unavailable_key", [None, "invalid-encryption-key"])
def test_list_fails_closed_when_credential_storage_is_unavailable(
    admin_client, app, unavailable_key
):
    app.state.settings.provider_credential_encryption_key = unavailable_key

    response = admin_client.get("/api/admin/provider-credentials")

    _assert_storage_unavailable(response)


@pytest.mark.parametrize("unavailable_key", [None, "invalid-encryption-key"])
def test_clear_fails_closed_without_mutating_credential_or_audit(
    admin_client, app, unavailable_key
):
    _enable_credential_storage(app)
    saved = admin_client.put(
        "/api/admin/provider-credentials/deepseek", json={"api_key": TEST_API_KEY}
    )
    assert saved.status_code == 200
    with app.state.session_factory() as session:
        before = session.get(ProviderCredentialRow, "deepseek")
        assert before is not None
        before_updated_at = before.updated_at
        assert before.state == "configured"
        assert before.encrypted_api_key is not None

    app.state.settings.provider_credential_encryption_key = unavailable_key
    response = admin_client.delete("/api/admin/provider-credentials/deepseek")

    _assert_storage_unavailable(response)
    with app.state.session_factory() as session:
        after = session.get(ProviderCredentialRow, "deepseek")
        assert after is not None
        assert after.state == "configured"
        assert after.key_hint == "...1234"
        assert after.updated_at == before_updated_at
        assert after.encrypted_api_key is not None
    assert _events(app, "provider.credentials.clear") == []
