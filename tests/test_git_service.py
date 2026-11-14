"""Local Git and GitHub service tests (no network)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import git_service
import github_service


def _configure_git(repo: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "OmniAgent Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )


def test_init_status_stage_and_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")

    initialized = git_service.init(repo)
    assert initialized["is_repo"] is True
    _configure_git(repo)

    status = git_service.status(repo)
    assert status["is_repo"] is True
    assert status["clean"] is False
    assert status["files"][0]["path"] == "hello.txt"
    assert status["files"][0]["untracked"] is True

    staged = git_service.stage(repo, files=["hello.txt"])
    assert staged["files"][0]["staged"] is True

    committed = git_service.commit(repo, message="test commit")
    assert committed["ok"] is True
    clean = git_service.status(repo)
    assert clean["clean"] is True
    assert clean["files"] == []


def test_workspace_inside_parent_repo_is_not_a_repo(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    git_service.init(parent)
    workspace = parent / "workspace"
    workspace.mkdir()

    status = git_service.status(workspace)
    assert status["is_repo"] is False
    assert status["clean"] is True


def test_branch_and_remote_validation() -> None:
    assert git_service._validate_branch("feature/test-1") == "feature/test-1"
    with pytest.raises(git_service.GitError):
        git_service._validate_branch("bad branch")

    assert (
        git_service._validate_remote_url("https://github.com/owner/repo.git")
        == "https://github.com/owner/repo.git"
    )
    with pytest.raises(git_service.GitError):
        git_service._validate_remote_url("https://user@github.com/owner/repo.git")
    with pytest.raises(git_service.GitError):
        git_service._validate_remote_url("https://evil.example/owner/repo.git")


def test_github_repository_validation_and_disconnected_account() -> None:
    assert github_service._validate_repo("owner/repo") == "owner/repo"
    with pytest.raises(github_service.GitHubError):
        github_service._validate_repo("owner")
    with pytest.raises(github_service.GitHubError):
        github_service._validate_repo("owner/repo/extra")

    account = github_service.public_account({})
    assert account["connected"] is False
    assert account["login"] == ""
