def test_task_routes_require_identity(client):
    assert client.get("/api/tasks").status_code == 401
    assert client.post(
        "/api/tasks",
        json={
            "goal": "Inspect authorized logs",
            "authorization_scope": "Uploaded logs only",
        },
    ).status_code == 401
    for method, path in [
        ("get", "/api/tasks/unknown"),
        ("get", "/api/tasks/unknown/report"),
        ("post", "/api/tasks/unknown/run"),
        ("post", "/api/tasks/unknown/pause"),
        ("post", "/api/tasks/unknown/resume"),
        ("post", "/api/tasks/unknown/retry"),
        ("post", "/api/tasks/unknown/cancel"),
        ("post", "/api/tasks/unknown/approve"),
    ]:
        response = getattr(client, method)(path)
        assert response.status_code == 401, path


def test_analyst_cannot_read_or_mutate_another_users_task(alice_client, bob_task):
    task_id = bob_task["id"]

    assert alice_client.get(f"/api/tasks/{task_id}").status_code == 403
    assert alice_client.post(f"/api/tasks/{task_id}/pause").status_code == 403


def test_admin_can_read_any_task(admin_client, bob_task):
    response = admin_client.get(f"/api/tasks/{bob_task['id']}")

    assert response.status_code == 200
    assert response.json()["owner_id"] == bob_task["owner_id"]


def test_analyst_list_contains_only_owned_tasks(alice_client, bob_client, bob_task):
    alice_task = alice_client.post(
        "/api/tasks",
        json={
            "goal": "Inspect Alice's logs",
            "authorization_scope": "Alice's logs only",
        },
    ).json()

    assert [task["id"] for task in alice_client.get("/api/tasks").json()] == [
        alice_task["id"]
    ]
    assert bob_client.get(f"/api/tasks/{alice_task['id']}").status_code == 403


def test_admin_list_contains_all_tasks(admin_client, alice_client, bob_task):
    alice_task = alice_client.post(
        "/api/tasks",
        json={
            "goal": "Inspect Alice's logs",
            "authorization_scope": "Alice's logs only",
        },
    ).json()

    ids = {task["id"] for task in admin_client.get("/api/tasks").json()}
    assert ids == {bob_task["id"], alice_task["id"]}


def test_missing_task_remains_not_found_for_authenticated_actor(alice_client):
    assert alice_client.get("/api/tasks/does-not-exist").status_code == 404
