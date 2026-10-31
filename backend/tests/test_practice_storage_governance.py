from fastapi.testclient import TestClient

from app.main import create_app


def test_demo_version_referenced_by_session_is_protected_by_cleanup_preview(temp_settings):
    with TestClient(create_app(temp_settings)) as client:
        demo_response = client.post("/api/practice/demo")
        assert demo_response.status_code == 200, demo_response.text
        demo = demo_response.json()["demo"]

        created = client.post(
            "/api/practice/sessions",
            json={
                "name": "protected demo",
                "start_date": demo["start_date"],
                "end_date": demo["end_date"],
                "cash_cents": 1_000_000,
                "positions": [],
                "data_version_id": demo["version_id"],
                "source_code": demo["source_code"],
                "settings": {},
                "idempotency_key": "storage-protected-demo-session",
            },
        )
        assert created.status_code == 200, created.text

        preview = client.post("/api/practice/storage/cleanup-preview", json={})

    assert preview.status_code == 200, preview.text
    value = preview.json()
    assert any(row["version_id"] == demo["version_id"] for row in value["protected"])
    assert all(row["version_id"] != demo["version_id"] for row in value["candidates"])
