from pathlib import Path

import pytest

from omni_agent.security import PathSecurityError, safe_join


def test_allows_nested_relative_path(tmp_path: Path) -> None:
    result = safe_join(tmp_path, "reports/q1.md")
    assert result == tmp_path.resolve() / "reports" / "q1.md"


def test_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(PathSecurityError):
        safe_join(tmp_path, "../../etc/passwd")


def test_rejects_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(PathSecurityError):
        safe_join(tmp_path, "/etc/passwd")


def test_rejects_prefix_sibling(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    sibling = tmp_path / "workspace2"
    root.mkdir()
    sibling.mkdir()
    with pytest.raises(PathSecurityError):
        safe_join(root, "../workspace2/secret.txt")


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathSecurityError):
        safe_join(root, "link/secret.txt")
