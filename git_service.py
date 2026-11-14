"""Native Git operations for the current project workspace.

This module intentionally runs outside the command sandbox: remote Git
operations need credentials and network access.  Security therefore comes from
a fixed argv (never a shell), workspace path containment, strict argument
validation, and an explicit token that is passed through a temporary askpass
helper instead of the command line.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from omni_agent.config import get_settings

BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
MAX_DIFF_CHARS = 200_000
MAX_LOG_ENTRIES = 50
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}


class GitError(RuntimeError):
    """Raised when a Git operation fails or is rejected."""


def _git_binary() -> str:
    binary = shutil.which("git")
    if not binary:
        raise GitError("系统未安装 git，请先安装 Git")
    return binary


def resolve_path(workspace: Path, relative: str = "") -> Path:
    """Resolve a path under the workspace, rejecting path traversal."""
    root = Path(workspace).resolve()
    candidate = (root / relative).resolve() if relative else root
    if candidate != root and root not in candidate.parents:
        raise GitError("路径不在当前工作区内")
    return candidate


def _repo_root(path: Path, workspace: Path) -> Optional[Path]:
    proc = subprocess.run(
        [_git_binary(), "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=15,
        env=_base_env(),
    )
    if proc.returncode != 0:
        return None
    top = Path(proc.stdout.strip()).resolve()
    root = Path(workspace).resolve()
    if top != root and root not in top.parents:
        return None
    return top


def _ensure_repo(path: Path, workspace: Path) -> Path:
    top = _repo_root(path, workspace)
    if top is None:
        raise GitError("当前目录不是 Git 仓库，请先初始化或克隆仓库")
    return top


def _base_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C.UTF-8"
    return env


def _run(
    args: Iterable[str],
    cwd: Path,
    *,
    timeout: int = 60,
    env: Optional[Dict[str, str]] = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [_git_binary(), "-C", str(cwd), *args]
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env or _base_env(),
    )
    if check and proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip()
        raise GitError(message or f"git 命令失败（退出码 {proc.returncode}）")
    return proc


def _askpass_env(token: str) -> tuple[Dict[str, str], str]:
    handle = tempfile.NamedTemporaryFile(
        mode="w", prefix="omni-git-askpass-", delete=False, encoding="utf-8"
    )
    script = (
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
        '  *) printf \'%s\\n\' "$OMNI_GIT_TOKEN" ;;\n'
        "esac\n"
    )
    handle.write(script)
    handle.close()
    os.chmod(handle.name, 0o700)
    env = _base_env()
    env["GIT_ASKPASS"] = handle.name
    env["OMNI_GIT_TOKEN"] = token
    return env, handle.name


def _remote_urls(cwd: Path) -> List[Dict[str, str]]:
    proc = _run(["remote", "-v"], cwd, check=False)
    remotes: List[Dict[str, str]] = []
    seen = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name, url, kind = parts[0], parts[1], parts[2].strip("()")
        key = (name, kind)
        if key in seen:
            continue
        seen.add(key)
        remotes.append({"name": name, "url": url, "kind": kind})
    return remotes


def _origin_url(remotes: List[Dict[str, str]]) -> str:
    for item in remotes:
        if item["name"] == "origin" and item["kind"] == "fetch":
            return item["url"]
    return ""


def _current_branch(cwd: Path) -> str:
    proc = _run(["branch", "--show-current"], cwd, check=False)
    return proc.stdout.strip()


def _validate_branch(branch: str) -> str:
    branch = str(branch or "").strip()
    if not branch or not BRANCH_RE.match(branch) or ".." in branch:
        raise GitError("分支名不合法")
    return branch


def _validate_remote_url(url: str) -> str:
    url = str(url or "").strip()
    if not url or len(url) > 2048 or url.startswith("-"):
        raise GitError("远程地址不合法")
    allowed = {
        item.strip().lower()
        for item in (get_settings().git_allowed_hosts or "").split(",")
        if item.strip()
    }
    if url.startswith("git@") and ":" in url:
        host = url[4:].split(":", 1)[0].lower()
        if host not in allowed:
            raise GitError(f"远程主机 {host} 不在允许列表中")
        return url
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "ssh"}:
        raise GitError("远程地址只支持 HTTPS 或 SSH")
    if parsed.username or parsed.password:
        raise GitError("远程地址不能包含账号或密码，请使用 GitHub Token")
    host = (parsed.hostname or "").lower()
    if host not in allowed:
        raise GitError(f"远程主机 {host} 不在允许列表中")
    return url


def _relative_files(repo: Path, files: Iterable[str]) -> List[str]:
    result: List[str] = []
    for raw in files:
        relative = str(raw or "").strip()
        if not relative or relative.startswith("-"):
            raise GitError(f"文件路径不合法: {relative}")
        candidate = (repo / relative).resolve()
        if candidate != repo and repo not in candidate.parents:
            raise GitError(f"文件路径不在仓库内: {relative}")
        result.append(candidate.relative_to(repo).as_posix())
    return result


def status(workspace: Path, relative: str = "") -> Dict[str, Any]:
    """Return repository state for the selected repository."""
    path = resolve_path(workspace, relative)
    top = _repo_root(path, workspace)
    base = {
        "path": str(path),
        "relative_path": path.relative_to(Path(workspace).resolve()).as_posix()
        if path != Path(workspace).resolve()
        else "",
        "is_repo": top is not None,
    }
    if top is None:
        return {**base, "root": str(path), "clean": True, "files": [], "remotes": []}

    proc = _run(["status", "--porcelain=v1", "--branch"], top)
    branch = ""
    upstream = ""
    ahead = 0
    behind = 0
    files: List[Dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        if line.startswith("## "):
            header = line[3:]
            if "..." in header:
                local, rest = header.split("...", 1)
                branch = local
                upstream = rest.split(" ", 1)[0]
            else:
                branch = header.split(" ", 1)[0]
            match = re.search(r"\[([^\]]+)\]", header)
            if match:
                for item in match.group(1).split(","):
                    item = item.strip()
                    if item.startswith("ahead "):
                        ahead = int(item.split()[1])
                    elif item.startswith("behind "):
                        behind = int(item.split()[1])
            continue
        if len(line) < 3:
            continue
        index_status, worktree_status = line[0], line[1]
        raw_path = line[3:].strip()
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ", 1)[1]
        files.append(
            {
                "path": raw_path,
                "index": index_status,
                "worktree": worktree_status,
                "staged": index_status not in {" ", "?", "!"},
                "untracked": index_status == "?" and worktree_status == "?",
            }
        )
    remotes = _remote_urls(top)
    return {
        **base,
        "root": str(top),
        "branch": branch or _current_branch(top),
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "clean": not files,
        "files": files,
        "remotes": remotes,
        "origin": _origin_url(remotes),
    }


def diff(workspace: Path, relative: str = "", *, staged: bool = False) -> Dict[str, Any]:
    path = resolve_path(workspace, relative)
    top = _ensure_repo(path, workspace)
    args = ["diff", "--no-color"]
    if staged:
        args.append("--cached")
    proc = _run(args, top, check=False)
    text = proc.stdout
    return {
        "diff": text[:MAX_DIFF_CHARS],
        "truncated": len(text) > MAX_DIFF_CHARS,
        "staged": staged,
    }


def log(workspace: Path, relative: str = "", limit: int = 20) -> List[Dict[str, str]]:
    path = resolve_path(workspace, relative)
    top = _ensure_repo(path, workspace)
    limit = max(1, min(int(limit), MAX_LOG_ENTRIES))
    proc = _run(
        [
            "log",
            f"-n{limit}",
            "--date=iso",
            "--pretty=format:%H%x1f%h%x1f%an%x1f%ae%x1f%ad%x1f%s",
        ],
        top,
        check=False,
    )
    entries: List[Dict[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 6:
            entries.append(
                {
                    "hash": parts[0],
                    "short": parts[1],
                    "author": parts[2],
                    "email": parts[3],
                    "date": parts[4],
                    "subject": parts[5],
                }
            )
    return entries


def branches(workspace: Path, relative: str = "") -> Dict[str, Any]:
    path = resolve_path(workspace, relative)
    top = _ensure_repo(path, workspace)
    current = _current_branch(top)
    proc = _run(
        [
            "for-each-ref",
            "--format=%(refname:short)%x1f%(upstream:short)%x1f%(objectname:short)",
            "refs/heads",
            "refs/remotes",
        ],
        top,
        check=False,
    )
    items: List[Dict[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3 and parts[0]:
            items.append(
                {
                    "name": parts[0],
                    "upstream": parts[1],
                    "commit": parts[2],
                    "current": parts[0] == current,
                }
            )
    return {"current": current, "branches": items}


def init(workspace: Path, relative: str = "") -> Dict[str, Any]:
    path = resolve_path(workspace, relative)
    _run(["init"], path)
    return status(workspace, relative)


def stage(workspace: Path, relative: str = "", files: Iterable[str] = ()) -> Dict[str, Any]:
    path = resolve_path(workspace, relative)
    top = _ensure_repo(path, workspace)
    selected = _relative_files(top, files)
    if not selected:
        _run(["add", "-A", "--"], top)
    else:
        _run(["add", "--", *selected], top)
    return status(workspace, relative)


def unstage(workspace: Path, relative: str = "", files: Iterable[str] = ()) -> Dict[str, Any]:
    path = resolve_path(workspace, relative)
    top = _ensure_repo(path, workspace)
    selected = _relative_files(top, files)
    if not selected:
        _run(["reset"], top, check=False)
    else:
        _run(["restore", "--staged", "--", *selected], top, check=False)
    return status(workspace, relative)


def commit(
    workspace: Path,
    relative: str = "",
    *,
    message: str,
    files: Iterable[str] = (),
) -> Dict[str, Any]:
    message = str(message or "").strip()
    if not message:
        raise GitError("提交信息不能为空")
    if len(message) > 5000:
        raise GitError("提交信息过长")
    path = resolve_path(workspace, relative)
    top = _ensure_repo(path, workspace)
    selected = _relative_files(top, files)
    if selected:
        _run(["add", "--", *selected], top)
    elif status(workspace, relative)["files"]:
        _run(["add", "-A", "--"], top)
    proc = _run(["commit", "-m", message], top, check=False)
    if proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout or "没有可提交的变更").strip())
    return {"ok": True, "message": message, "output": proc.stdout.strip()[:2000]}


def checkout(
    workspace: Path, relative: str = "", *, branch: str, create: bool = False
) -> Dict[str, Any]:
    branch = _validate_branch(branch)
    path = resolve_path(workspace, relative)
    top = _ensure_repo(path, workspace)
    args = ["checkout", "-b" if create else "", branch]
    args = [item for item in args if item]
    _run(args, top)
    return status(workspace, relative)


def set_remote(
    workspace: Path, relative: str = "", *, name: str = "origin", url: str
) -> Dict[str, Any]:
    name = str(name or "origin").strip()
    if not re.match(r"^[A-Za-z0-9._-]{1,64}$", name):
        raise GitError("远程名称不合法")
    url = _validate_remote_url(url)
    path = resolve_path(workspace, relative)
    top = _ensure_repo(path, workspace)
    existing = [item["name"] for item in _remote_urls(top)]
    if name in existing:
        _run(["remote", "set-url", name, url], top)
    else:
        _run(["remote", "add", name, url], top)
    return status(workspace, relative)


def _network(
    workspace: Path,
    relative: str,
    args: List[str],
    *,
    token: str,
    timeout: int = 120,
) -> Dict[str, Any]:
    path = resolve_path(workspace, relative)
    top = _ensure_repo(path, workspace)
    env = _base_env()
    temp_path = ""
    if token:
        env, temp_path = _askpass_env(token)
    try:
        proc = _run(
            ["-c", "credential.helper=", *args],
            top,
            timeout=timeout,
            env=env,
            check=False,
        )
    finally:
        if temp_path:
            with contextlib.suppress(OSError):
                os.unlink(temp_path)
    if proc.returncode != 0:
        text = (proc.stderr or proc.stdout or "").strip()
        raise GitError(text or "Git 网络操作失败")
    return {
        "ok": True,
        "output": (proc.stdout or proc.stderr or "").strip()[:4000],
        "status": status(workspace, relative),
    }


def push(
    workspace: Path,
    relative: str = "",
    *,
    branch: str = "",
    token: str = "",
) -> Dict[str, Any]:
    path = resolve_path(workspace, relative)
    top = _ensure_repo(path, workspace)
    branch = _validate_branch(branch or _current_branch(top))
    return _network(
        workspace,
        relative,
        ["push", "-u", "origin", branch],
        token=token,
    )


def pull(
    workspace: Path,
    relative: str = "",
    *,
    branch: str = "",
    token: str = "",
) -> Dict[str, Any]:
    path = resolve_path(workspace, relative)
    top = _ensure_repo(path, workspace)
    branch = _validate_branch(branch or _current_branch(top))
    return _network(
        workspace,
        relative,
        ["pull", "--ff-only", "origin", branch],
        token=token,
    )


def fetch(
    workspace: Path,
    relative: str = "",
    *,
    token: str = "",
) -> Dict[str, Any]:
    return _network(workspace, relative, ["fetch", "--all", "--prune"], token=token)


def list_repos(workspace: Path) -> List[Dict[str, Any]]:
    root = Path(workspace).resolve()
    result: List[Dict[str, Any]] = []
    if (root / ".git").exists():
        result.append(
            {
                "path": "",
                "name": root.name,
                "root": str(root),
                "branch": _current_branch(root),
            }
        )
    for current, dirs, _files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        depth = len(Path(current).relative_to(root).parts)
        if depth >= 3:
            dirs[:] = []
        if Path(current) == root:
            continue
        if (Path(current) / ".git").exists():
            rel = Path(current).relative_to(root).as_posix()
            result.append(
                {
                    "path": rel,
                    "name": Path(current).name,
                    "root": str(Path(current).resolve()),
                    "branch": _current_branch(Path(current)),
                }
            )
            dirs[:] = []
    result.sort(key=lambda item: item["path"])
    return result


def clone(
    workspace: Path,
    *,
    url: str,
    target: str,
    token: str,
) -> Dict[str, Any]:
    url = _validate_remote_url(url)
    root = Path(workspace).resolve()
    destination = resolve_path(root, target)
    if destination.exists() and any(destination.iterdir()):
        raise GitError("目标目录已存在且不为空")
    destination.parent.mkdir(parents=True, exist_ok=True)
    env = _base_env()
    temp_path = ""
    if token:
        env, temp_path = _askpass_env(token)
    try:
        proc = subprocess.run(
            [
                _git_binary(),
                "-c",
                "credential.helper=",
                "clone",
                url,
                str(destination),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
    finally:
        if temp_path:
            with contextlib.suppress(OSError):
                os.unlink(temp_path)
    if proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout or "克隆失败").strip())
    return {
        "ok": True,
        "path": destination.relative_to(root).as_posix(),
        "root": str(destination.resolve()),
    }
