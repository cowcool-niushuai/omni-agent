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


def test_security_policy_endpoint() -> None:
    response = client.get("/api/security/policy")
    assert response.status_code == 200
    body = response.json()
    assert body["sandbox_backend"] in {"process", "bwrap"}
    assert body["command_policy_mode"] in {"strict", "permissive"}
    assert body["allow_private_network"] is False


def test_audit_endpoint() -> None:
    response = client.get("/api/audit", params={"limit": 5})
    assert response.status_code == 200
    assert isinstance(response.json(), list)
    stats = client.get("/api/audit/stats")
    assert stats.status_code == 200
    assert isinstance(stats.json(), list)


def test_knowledge_endpoint() -> None:
    response = client.get("/api/knowledge")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
