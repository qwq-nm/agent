def test_system_status_lists_provider_configuration_and_registered_tools(client) -> None:
    models = client.get("/api/models/status")
    tools = client.get("/api/tools")
    assert models.status_code == tools.status_code == 200
    assert {item["name"] for item in models.json()} >= {"mock"}
    assert all(
        {"name", "scene", "risk_level"} <= item.keys() for item in tools.json()
    )
