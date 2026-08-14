import io
import json
import zipfile


def test_source_zip_is_statically_audited_with_masked_evidence(
    analyst_client, run_queued_job
) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(
            "vulnerable_app/app.py",
            'import os\nAPI_KEY = "sk-demo-not-real-123456"\n'
            'def run(cmd):\n    return os.system(cmd)\n',
        )
        output.writestr("vulnerable_app/config.py", "DEBUG = True\n")
    archive.seek(0)
    created = analyst_client.post(
        "/api/tasks",
        data={
            "payload": json.dumps(
                {
                    "goal": "静态审计上传的源码",
                    "authorization_scope": "仅静态读取上传文件，不执行源码",
                    "route_mode": "auto",
                    "scene_hint": "source_audit",
                },
                ensure_ascii=False,
            )
        },
        files={"file": ("vulnerable_app.zip", archive.getvalue(), "application/zip")},
    ).json()

    response = analyst_client.post(
        f"/api/tasks/{created['id']}/run",
        headers={"Idempotency-Key": "source-run-001"},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    run_queued_job()
    detail = analyst_client.get(f"/api/tasks/{created['id']}").json()
    assert detail["status"] == "completed"
    report = analyst_client.get(f"/api/tasks/{created['id']}/report").text
    assert "PY-CMD-001" in report
    assert "CFG-DEBUG-001" in report
    assert "app.py:" in report
    assert "sk-demo-not-real-123456" not in report
    assert "***REDACTED***" in report
