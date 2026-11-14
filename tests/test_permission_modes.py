from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import database
import permission
import server
import tools
from omni_agent import tenancy
from omni_agent.security import commands
from omni_agent.tenancy import TenantContext


@pytest.fixture
def full_access_context(tmp_path: Path):
    token = tenancy.set_context(
        TenantContext(workspace=tmp_path, permission_mode="full_access")
    )
    try:
        yield tmp_path
    finally:
        tenancy.reset_context(token)


@pytest.fixture
def strict_context(tmp_path: Path):
    token = tenancy.set_context(
        TenantContext(workspace=tmp_path, permission_mode="strict")
    )
    try:
        yield tmp_path
    finally:
        tenancy.reset_context(token)


def test_full_access_allows_host_shell_but_keeps_system_paths_blocked(
    full_access_context: Path,
) -> None:
    allowed = commands.assess_command(
        "echo hello | cat",
        full_access_context,
        policy_mode="full_access",
    )
    assert allowed.risk == commands.ALLOW

    system_path = commands.assess_command(
        "cat /etc/passwd",
        full_access_context,
        policy_mode="full_access",
    )
    assert system_path.risk == commands.DENY

    catastrophic = commands.assess_command(
        "rm -rf /",
        full_access_context,
        policy_mode="full_access",
    )
    assert catastrophic.risk == commands.DENY


def test_full_access_converts_ask_user_to_allow(full_access_context: Path) -> None:
    risk, reason, request_id = permission.check_permission(
        "bash_run",
        {"command": "git push origin main"},
        "sess_full",
        persist=False,
    )
    assert risk == permission.PermissionRisk.ALLOW
    assert request_id == ""
    assert "完全访问" in reason


def test_full_access_file_write_outside_workspace_is_allowed(
    full_access_context: Path,
) -> None:
    target = str(full_access_context.parent / "outside.txt")
    risk, _, _ = permission.check_permission(
        "file_write",
        {"filepath": target, "content": "hello"},
        "sess_full",
        persist=False,
    )
    assert risk == permission.PermissionRisk.SAFE_EXEC
    assert tools.resolve_path(target) == str(Path(target).resolve())


def test_full_access_still_protects_system_writes(full_access_context: Path) -> None:
    assert tools._check_protected_write(Path("/etc/passwd")) is not None
    assert tools._check_protected_write(Path("/tmp/omni-ok.txt")) is None


def test_strict_mode_denies_outside_workspace_write(strict_context: Path) -> None:
    target = str(strict_context.parent / "outside.txt")
    risk, _, _ = permission.check_permission(
        "file_write",
        {"filepath": target, "content": "hello"},
        "sess_strict",
        persist=False,
    )
    assert risk == permission.PermissionRisk.DENY


def test_security_policy_endpoint_roundtrip() -> None:
    client = TestClient(server.app)
    original = client.get("/api/security/policy").json()["permission_mode"]
    try:
        enabled = client.patch(
            "/api/security/policy", json={"permission_mode": "full_access"}
        )
        assert enabled.status_code == 200
        assert enabled.json()["permission_mode"] == "full_access"
        assert enabled.json()["sandbox_backend"] == "host"
        assert enabled.json()["sandbox_network"] is True
        assert enabled.json()["command_policy_mode"] == "full_access"

        restored = client.patch(
            "/api/security/policy", json={"permission_mode": "strict"}
        )
        assert restored.status_code == 200
        assert restored.json()["permission_mode"] == "strict"
        assert restored.json()["sandbox_network"] is False
        assert database.get_project_setting(
            tenancy.DEFAULT_PROJECT_ID, "permission_mode", "strict"
        ) == "strict"
    finally:
        client.patch("/api/security/policy", json={"permission_mode": original})
