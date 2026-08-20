from fastapi.testclient import TestClient


def _assert_envelope(response, status, code):
    assert response.status_code == status
    assert set(response.json()) == {"error"}
    error = response.json()["error"]
    assert set(error) == {"code", "message", "fields", "trace_id"}
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["fields"] is None or isinstance(error["fields"], list)
    assert isinstance(error["trace_id"], str) and error["trace_id"]
    assert response.headers["x-trace-id"] == error["trace_id"]


def test_authentication_authorization_missing_and_validation_use_error_envelope(
    client, alice_client, bob_task
):
    _assert_envelope(client.get("/api/tasks"), 401, "unauthorized")
    _assert_envelope(
        alice_client.get(f"/api/tasks/{bob_task['id']}"), 403, "forbidden"
    )
    _assert_envelope(
        alice_client.get("/api/tasks/does-not-exist"), 404, "not_found"
    )
    _assert_envelope(
        alice_client.post(
            "/api/tasks",
            json={"goal": "x", "authorization_scope": "too short"},
        ),
        422,
        "validation_error",
    )


def test_unexpected_error_is_generic_and_does_not_leak_stack_or_external_body(app):
    secret = "RAW_PROVIDER_BODY_SHOULD_NEVER_LEAK"

    @app.get("/api/test-unexpected")
    def fail():
        raise RuntimeError(secret)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/test-unexpected")

    _assert_envelope(response, 500, "internal_error")
    assert secret not in response.text
    assert "Traceback" not in response.text


def test_framework_generated_404_and_405_use_error_envelope(client):
    _assert_envelope(client.get("/route-that-does-not-exist"), 404, "not_found")
    _assert_envelope(client.delete("/api/health"), 405, "method_not_allowed")


def test_malformed_json_is_validation_error_without_body_echo(analyst_client):
    malformed = '{"goal":"do not echo this",'

    response = analyst_client.post(
        "/api/tasks",
        content=malformed,
        headers={"content-type": "application/json"},
    )

    _assert_envelope(response, 422, "validation_error")
    assert malformed not in response.text


def test_login_rejects_oversized_identifier_before_audit_storage(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "x" * 256, "password": "wrong-password"},
    )

    _assert_envelope(response, 422, "validation_error")
