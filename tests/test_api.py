import uuid

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
    assert body["active"]["ref"]
    assert body["active"]["adapter"] in {"openai-compatible", "anthropic"}
    assert all(model["ref"] for model in body["models"])


def test_unknown_model_is_rejected() -> None:
    response = client.post("/api/models/active", json={"model_id": "missing/model"})
    assert response.status_code == 404


def test_model_probe_without_key_is_reported() -> None:
    response = client.post("/api/models/test", json={"provider_key": "not-a-provider"})
    assert response.status_code == 200
    assert response.json()["ok"] is False


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


def test_memory_crud_and_search() -> None:
    key = f"test_memory_{uuid.uuid4().hex[:8]}"
    created = client.post(
        "/api/memories",
        json={
            "category": "fact",
            "key": key,
            "content": "用户偏好使用本地 BM25 检索中文知识库。",
            "importance": 3,
        },
    )
    assert created.status_code == 200
    memory = created.json()
    memory_id = memory["id"]
    assert memory["key"] == key

    try:
        found = client.get("/api/memories", params={"q": "BM25 知识库"})
        assert found.status_code == 200
        assert any(item["id"] == memory_id for item in found.json())

        updated = client.patch(
            f"/api/memories/{memory_id}",
            json={"content": "更新后的记忆内容。", "importance": 4},
        )
        assert updated.status_code == 200
        assert updated.json()["content"] == "更新后的记忆内容。"
        assert updated.json()["importance"] == 4

        empty = client.post(
            "/api/memories",
            json={"category": "fact", "key": "", "content": "x"},
        )
        assert empty.status_code == 400
    finally:
        deleted = client.delete(f"/api/memories/{memory_id}")
        assert deleted.status_code == 200

    assert client.delete(f"/api/memories/{memory_id}").status_code == 404


def test_knowledge_crud_and_search() -> None:
    title = f"测试知识文档 {uuid.uuid4().hex[:8]}"
    created = client.post(
        "/api/knowledge",
        json={
            "title": title,
            "content": "这是一篇关于 MCP JSON-RPC 协议与工具调用的测试文档。",
            "tags": "mcp,test",
        },
    )
    assert created.status_code == 200
    doc = created.json()
    doc_id = doc["id"]
    assert doc["title"] == title

    try:
        found = client.get("/api/knowledge", params={"q": "JSON-RPC 协议"})
        assert found.status_code == 200
        assert any(item["id"] == doc_id for item in found.json())

        updated = client.patch(
            f"/api/knowledge/{doc_id}",
            json={"tags": "mcp,updated"},
        )
        assert updated.status_code == 200
        assert updated.json()["tags"] == "mcp,updated"

        empty = client.post(
            "/api/knowledge",
            json={"title": "x", "content": ""},
        )
        assert empty.status_code == 400
    finally:
        deleted = client.delete(f"/api/knowledge/{doc_id}")
        assert deleted.status_code == 200

    assert client.delete(f"/api/knowledge/{doc_id}").status_code == 404


def test_schedule_crud_and_validation() -> None:
    name = f"测试定时任务 {uuid.uuid4().hex[:8]}"
    created = client.post(
        "/api/schedules",
        json={
            "name": name,
            "prompt": "检查工作区并总结变化。",
            "interval_seconds": 120,
        },
    )
    assert created.status_code == 200
    task = created.json()
    task_id = task["id"]
    assert task["enabled"] is True
    assert task["session_id"].startswith("sched_")

    try:
        listed = client.get("/api/schedules")
        assert listed.status_code == 200
        assert any(item["id"] == task_id for item in listed.json())

        fetched = client.get(f"/api/schedules/{task_id}")
        assert fetched.status_code == 200
        assert fetched.json()["name"] == name

        updated = client.patch(
            f"/api/schedules/{task_id}",
            json={"name": name + " v2", "enabled": False},
        )
        assert updated.status_code == 200
        assert updated.json()["enabled"] is False
        assert updated.json()["name"].endswith(" v2")

        too_fast = client.post(
            "/api/schedules",
            json={"name": "x", "prompt": "x", "interval_seconds": 1},
        )
        assert too_fast.status_code == 400

        empty = client.patch(f"/api/schedules/{task_id}", json={"prompt": ""})
        assert empty.status_code == 400
    finally:
        deleted = client.delete(f"/api/schedules/{task_id}")
        assert deleted.status_code == 200

    assert client.delete(f"/api/schedules/{task_id}").status_code == 404
