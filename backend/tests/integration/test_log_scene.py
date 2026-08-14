import json
from pathlib import Path


FIXTURE = Path(__file__).parents[1] / "fixtures" / "access_attack.log"


def test_uploaded_log_scene_produces_traceable_scan_report(analyst_client) -> None:
    with FIXTURE.open("rb") as source:
        created = analyst_client.post(
            "/api/tasks",
            data={
                "payload": json.dumps(
                    {
                        "goal": "分析上传的访问日志并识别攻击行为",
                        "authorization_scope": "仅分析上传文件",
                        "route_mode": "auto",
                        "scene_hint": "incident_response",
                    },
                    ensure_ascii=False,
                )
            },
            files={"file": ("access_attack.log", source, "text/plain")},
        ).json()

    response = analyst_client.post(f"/api/tasks/{created['id']}/run")
    assert response.status_code == 202
    detail = analyst_client.get(f"/api/tasks/{created['id']}").json()
    assert detail["status"] == "completed"
    assert any(
        item["source"].startswith("access_attack.log:")
        for item in detail["evidences"]
    )
    report = analyst_client.get(f"/api/tasks/{created['id']}/report").text
    assert "WEB-SCAN-002" in report
    assert "access_attack.log:" in report
