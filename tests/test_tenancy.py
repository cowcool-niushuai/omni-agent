"""Tenant, authentication, RBAC and project-isolation tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import database
import permission
import server
from omni_agent import tenancy


@pytest.fixture
def tenant_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "tenant.db"))
    monkeypatch.setattr(server.settings, "auth_required", True)
    monkeypatch.setattr(server.settings, "admin_password", "admin-pass-123")
    # Keep both the legacy default workspace and per-project workspaces inside
    # the test's temporary directory.
    monkeypatch.setattr(server.settings, "workspace_dir", tmp_path / "workspace")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    database.init_db()

    admin = database.get_user_by_username("admin")
    assert admin is not None
    team_a = database.create_team("Team A", admin["id"])
    team_b = database.create_team("Team B", admin["id"])
    project_a = database.create_project(team_a["id"], "Project A", admin["id"])
    project_b = database.create_project(team_b["id"], "Project B", admin["id"])
    alice = database.create_user(
        "alice", "alice-pass-123", display_name="Alice", role="member"
    )
    viewer = database.create_user(
        "viewer", "viewer-pass-123", display_name="Viewer", role="viewer"
    )
    database.add_team_member(team_a["id"], alice["id"], "member")
    database.add_team_member(team_a["id"], viewer["id"], "member")
    return {
        "admin": admin,
        "alice": alice,
        "viewer": viewer,
        "team_a": team_a,
        "team_b": team_b,
        "project_a": project_a,
        "project_b": project_b,
    }


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


def test_password_hash_and_user_crud(tenant_env: dict[str, Any]) -> None:
    user = database.get_user_by_username("alice")
    assert user is not None
    stored = database.get_user(user["id"])
    assert stored is not None
    assert "password_hash" not in stored
    assert database.authenticate_user("alice", "alice-pass-123") is not None
    assert database.authenticate_user("alice", "wrong") is None

    with pytest.raises(ValueError):
        database.create_user("bad name", "12345678")
    with pytest.raises(ValueError):
        database.create_user("short", "123")


def test_project_scoped_sessions_and_memories(tenant_env: dict[str, Any]) -> None:
    project_a = tenant_env["project_a"]["id"]
    project_b = tenant_env["project_b"]["id"]
    alice = tenant_env["alice"]["id"]

    session = database.create_session(
        "sess_a", owner_id=alice, project_id=project_a
    )
    assert session["project_id"] == project_a
    assert database.get_session_tenant("sess_a")["project_id"] == project_a
    assert database.list_sessions(project_id=project_a)
    assert database.list_sessions(project_id=project_b) == []

    memory = database.save_memory(
        "fact", "k8s", "使用 Kubernetes 部署", project_id=project_a
    )
    assert memory["project_id"] == project_a
    assert database.get_memory(memory["id"], project_id=project_b) is None
    assert database.get_all_memories(project_id=project_b) == []

    doc = database.add_knowledge(
        "Runbook", "部署说明", project_id=project_a
    )
    assert database.get_knowledge(doc["id"], project_id=project_b) is None
    assert database.list_knowledge(project_id=project_b) == []


def test_login_projects_and_csrf(tenant_env: dict[str, Any]) -> None:
    client = TestClient(server.app)
    csrf = _login(client, "alice", "alice-pass-123")
    project_a = tenant_env["project_a"]["id"]

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "alice"
    assert {p["id"] for p in me.json()["projects"]} == {project_a}

    denied = client.post(
        "/api/sessions",
        json={"title": "test"},
        headers={"X-Project-ID": project_a},
    )
    assert denied.status_code == 403

    created = client.post(
        "/api/sessions",
        json={"title": "test"},
        headers={"X-Project-ID": project_a, "X-CSRF-Token": csrf},
    )
    assert created.status_code == 200, created.text
    assert created.json()["project_id"] == project_a


def test_cross_project_access_is_blocked(tenant_env: dict[str, Any]) -> None:
    client = TestClient(server.app)
    _login(client, "alice", "alice-pass-123")
    project_b = tenant_env["project_b"]["id"]

    response = client.get(
        "/api/sessions", headers={"X-Project-ID": project_b}
    )
    assert response.status_code == 403


def test_viewer_cannot_write(tenant_env: dict[str, Any]) -> None:
    client = TestClient(server.app)
    csrf = _login(client, "viewer", "viewer-pass-123")
    project_a = tenant_env["project_a"]["id"]
    response = client.post(
        "/api/sessions",
        json={"title": "blocked"},
        headers={"X-Project-ID": project_a, "X-CSRF-Token": csrf},
    )
    assert response.status_code == 403


def test_api_token_scopes(tenant_env: dict[str, Any]) -> None:
    admin = tenant_env["admin"]
    token = database.create_api_token(
        admin["id"], "read-only", scopes=["sessions:read"], expires_days=1
    )
    client = TestClient(server.app)
    headers = {"Authorization": f"Bearer {token['token']}"}
    assert client.get("/api/sessions", headers=headers).status_code == 200
    denied = client.post("/api/sessions", json={"title": "x"}, headers=headers)
    assert denied.status_code == 403


def test_last_admin_cannot_be_demoted(tenant_env: dict[str, Any]) -> None:
    client = TestClient(server.app)
    csrf = _login(client, "admin", "admin-pass-123")
    admin = tenant_env["admin"]
    response = client.patch(
        f"/api/users/{admin['id']}",
        json={"role": "member"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400


def test_user_without_project_has_no_default_access(
    tenant_env: dict[str, Any],
) -> None:
    database.create_user("outsider", "outsider-pass-123", role="member")
    client = TestClient(server.app)
    _login(client, "outsider", "outsider-pass-123")
    response = client.get("/api/sessions")
    assert response.status_code == 403


def test_permission_requests_are_project_scoped(
    tenant_env: dict[str, Any],
) -> None:
    project_a = tenant_env["project_a"]["id"]
    project_b = tenant_env["project_b"]["id"]
    token = tenancy.set_context(
        user_id=tenant_env["alice"]["id"],
        project_id=project_a,
        team_id=tenant_env["team_a"]["id"],
        role="member",
        team_role="member",
    )
    try:
        risk, _, request_id = permission.check_permission(
            "bash_run", {"command": "chmod 600 notes.txt"}, "sess_pending"
        )
    finally:
        tenancy.reset_context(token)
    assert risk == "ASK_USER"
    stored = permission.get_request(request_id)
    assert stored is not None
    assert stored["project_id"] == project_a
    assert permission.pending_request_ids(
        "sess_pending", project_id=project_b
    ) == []
