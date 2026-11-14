from pathlib import Path

import pytest

import database
from omni_agent.security.workspace_folders import (
    WorkspaceFolderError,
    validate_workspace_folder,
)


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "folders.db"))
    database.init_db()
    return tmp_path


def test_validate_workspace_folder(tmp_path: Path) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    assert validate_workspace_folder(str(folder)) == folder.resolve()

    with pytest.raises(WorkspaceFolderError):
        validate_workspace_folder("relative/path")
    with pytest.raises(WorkspaceFolderError):
        validate_workspace_folder("/etc")
    with pytest.raises(WorkspaceFolderError):
        validate_workspace_folder(str(folder / "missing"))


def test_project_uses_selected_folder(isolated_db: Path, tmp_path: Path) -> None:
    folder = tmp_path / "workspace-folder"
    folder.mkdir()
    project = database.create_project(
        "team_default",
        "本地项目",
        "usr_admin",
        workspace_path=str(folder),
    )
    assert project["workspace_path"] == str(folder.resolve())
    assert database.get_project_workspace(project["id"]) == folder.resolve()

    found = database.find_project_by_workspace(folder)
    assert found is not None
    assert found["id"] == project["id"]
