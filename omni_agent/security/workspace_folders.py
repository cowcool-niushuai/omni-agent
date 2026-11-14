"""Validation for user-selected project folders.

The desktop client can hand the backend an arbitrary absolute directory.  That
is a privileged operation: keep system locations out of reach and require the
folder to exist and be writable before it becomes a project workspace.
"""

from __future__ import annotations

import os
from pathlib import Path

DENIED_ROOTS = {
    Path("/bin"),
    Path("/boot"),
    Path("/dev"),
    Path("/etc"),
    Path("/lib"),
    Path("/lib64"),
    Path("/opt"),
    Path("/proc"),
    Path("/root"),
    Path("/run"),
    Path("/sbin"),
    Path("/sys"),
    Path("/usr"),
    Path("/var"),
}


class WorkspaceFolderError(ValueError):
    """Raised when a folder cannot be used as a project workspace."""


def validate_workspace_folder(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value).strip()
    if not raw:
        raise WorkspaceFolderError("请选择项目文件夹")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise WorkspaceFolderError("请选择绝对路径文件夹")
    try:
        folder = candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceFolderError(f"文件夹不存在或无法访问: {raw}") from exc
    if not folder.is_dir():
        raise WorkspaceFolderError("选择的路径不是文件夹")
    if folder == Path("/"):
        raise WorkspaceFolderError("不能使用系统根目录作为项目文件夹")
    for denied in DENIED_ROOTS:
        if folder == denied or denied in folder.parents:
            raise WorkspaceFolderError(f"不能使用系统目录作为项目文件夹: {folder}")
    if not os.access(folder, os.W_OK | os.X_OK):
        raise WorkspaceFolderError("文件夹不可写，请选择有写入权限的目录")
    return folder
