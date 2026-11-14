"""Command policy engine tests (no execution)."""

from __future__ import annotations

from pathlib import Path

import permission
from omni_agent.security.commands import (
    ALLOW,
    ASK_USER,
    DENY,
    SAFE_EXEC,
    assess_command,
    validate_sql_readonly,
)


def risk(command: str, root: Path, mode: str = "strict") -> str:
    return assess_command(command, root, policy_mode=mode).risk


def test_read_only_command_is_allowed(tmp_path: Path) -> None:
    assert risk("ls -la", tmp_path) == ALLOW
    assert risk("git status", tmp_path) == ALLOW
    assert risk("sed -n 1p notes.txt", tmp_path) == ALLOW


def test_write_command_is_safe_exec(tmp_path: Path) -> None:
    assert risk("mkdir output", tmp_path) == SAFE_EXEC
    assert risk("python -c \"print(1)\"", tmp_path) == SAFE_EXEC
    assert risk("sed -i s/a/b/ notes.txt", tmp_path) == SAFE_EXEC


def test_rm_requires_approval(tmp_path: Path) -> None:
    assert risk("rm -rf /tmp/omni-policy-test", tmp_path) == ASK_USER


def test_root_deletion_is_denied(tmp_path: Path) -> None:
    assert risk("rm -rf /", tmp_path) == DENY
    assert risk("rm -rf /*", tmp_path) == DENY


def test_workspace_escape_is_denied(tmp_path: Path) -> None:
    assert risk("cat ../secret.txt", tmp_path) == DENY
    assert risk("cp safe.txt ../outside.txt", tmp_path) == DENY


def test_system_paths_are_denied(tmp_path: Path) -> None:
    assert risk("cat /etc/passwd", tmp_path) == DENY
    assert risk("ls /root", tmp_path) == DENY


def test_shell_operators_are_denied_in_strict_mode(tmp_path: Path) -> None:
    assert risk("ls | sh", tmp_path) == DENY
    assert risk("echo hi; rm -rf /tmp/x", tmp_path) == DENY
    assert risk("echo $(whoami)", tmp_path) == DENY
    assert risk("echo hi > out.txt", tmp_path) == DENY


def test_shell_operators_require_approval_in_permissive_mode(tmp_path: Path) -> None:
    assessment = assess_command("echo hi | wc -c", tmp_path, policy_mode="permissive")
    assert assessment.risk == ASK_USER
    assert assessment.uses_shell_syntax is True


def test_shell_c_is_denied(tmp_path: Path) -> None:
    assert risk("bash -c id", tmp_path) == DENY
    assert risk("sh -c 'cat /etc/passwd'", tmp_path) == DENY


def test_system_admin_commands_are_denied(tmp_path: Path) -> None:
    assert risk("sudo ls", tmp_path) == DENY
    assert risk("systemctl restart nginx", tmp_path) == DENY


def test_unknown_executable_requires_approval(tmp_path: Path) -> None:
    assert risk("totally-unknown-binary --help", tmp_path) == ASK_USER


def test_network_and_destructive_subcommands_ask(tmp_path: Path) -> None:
    assert risk("git push origin main", tmp_path) == ASK_USER
    assert risk("pip install requests", tmp_path) == ASK_USER
    assert risk("python -m pip install requests", tmp_path) == ASK_USER
    assert risk("find . -name x -delete", tmp_path) == ASK_USER


def test_fork_bomb_is_denied(tmp_path: Path) -> None:
    assert risk(":(){ :|:& };:", tmp_path) == DENY


def test_sql_readonly_policy() -> None:
    assert validate_sql_readonly("SELECT * FROM users")[0] is True
    assert validate_sql_readonly("SELECT 1; DROP TABLE users")[0] is False
    assert validate_sql_readonly("PRAGMA table_info(users)")[0] is False
    assert validate_sql_readonly("WITH x AS (SELECT 1) SELECT * FROM x")[0] is False


def test_permission_classifier_uses_shared_policy() -> None:
    assert permission.classify_permission("bash_run", {"command": "ls"})[0] == "ALLOW"
    assert (
        permission.classify_permission(
            "bash_run", {"command": "rm -rf /tmp/omni-policy-test"}
        )[0]
        == "ASK_USER"
    )
    assert (
        permission.classify_permission("file_write", {"filepath": "../evil", "content": "x"})[0]
        == "DENY"
    )
    assert (
        permission.classify_permission("web_fetch", {"url": "http://127.0.0.1/"})[0]
        == "DENY"
    )
