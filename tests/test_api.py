from fastapi.testclient import TestClient

import server

client = TestClient(server.app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["service"] == "omni-agent"


def test_models_response_has_no_secret() -> None:
    response = client.get("/api/models")
    assert response.status_code == 200
    body = response.json()
    assert "api_key" not in body["active"]
    assert all("api_key" not in model for model in body["models"])


def test_workspace_traversal_is_blocked() -> None:
    response = client.get("/api/workspace/file", params={"path": "../../etc/passwd"})
    assert response.status_code == 403


def test_skill_id_traversal_is_rejected() -> None:
    response = client.post(
        "/api/skills",
        json={
            "skill_id": "../evil",
            "name": "x",
            "description": "x",
            "content": "x",
        },
    )
    assert response.status_code == 400
