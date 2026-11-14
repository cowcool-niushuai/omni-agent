"""GitHub account and repository operations.

Tokens are stored per user below ``DATA_DIR/credentials`` with mode 0600 and
are never returned by the API or written to prompts.  All calls use the fixed
GitHub REST API host; repository names are validated before they reach a URL.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from omni_agent.config import get_settings
from omni_agent.tenancy import current_user_id

GITHUB_API = "https://api.github.com"
_TIMEOUT = 20.0
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _safe_user(user_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", user_id or "anonymous")


def _credential_path(user_id: Optional[str] = None) -> Path:
    settings = get_settings()
    user = _safe_user(user_id or current_user_id())
    directory = Path(settings.data_dir) / "credentials"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory / f"github_{user}.json"


def _read_credential(user_id: Optional[str] = None) -> Dict[str, Any]:
    path = _credential_path(user_id)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_credential(data: Dict[str, Any]) -> None:
    path = _credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)


def load_token() -> str:
    return str(_read_credential().get("token") or "")


def _validate_repo(repo: str) -> str:
    repo = str(repo or "").strip()
    if not _REPO_RE.fullmatch(repo):
        raise GitHubError("仓库格式应为 owner/repo")
    return repo


def _request(
    method: str,
    path: str,
    *,
    token: Optional[str] = None,
    json_body: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> httpx.Response:
    token = token or load_token()
    if not token:
        raise GitHubError("尚未配置 GitHub Token", status=401)
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "OmniAgent/0.1",
    }
    try:
        with httpx.Client(timeout=_TIMEOUT, trust_env=False) as client:
            response = client.request(
                method,
                f"{GITHUB_API}{path}",
                headers=headers,
                json=json_body,
                params=params,
            )
    except httpx.HTTPError as exc:
        raise GitHubError(f"连接 GitHub 失败: {exc}") from exc
    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        message = str(payload.get("message") or response.text[:300] or "GitHub 请求失败")
        raise GitHubError(message, status=response.status_code)
    return response


def connect(token: str) -> Dict[str, Any]:
    token = str(token or "").strip()
    if not token:
        raise GitHubError("Token 不能为空")
    if len(token) > 500:
        raise GitHubError("Token 格式不正确")
    response = _request("GET", "/user", token=token)
    user = response.json()
    scopes = response.headers.get("x-oauth-scopes", "")
    data = {
        "token": token,
        "login": str(user.get("login") or ""),
        "id": user.get("id"),
        "name": str(user.get("name") or ""),
        "avatar_url": str(user.get("avatar_url") or ""),
        "scopes": [item.strip() for item in scopes.split(",") if item.strip()],
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_credential(data)
    return public_account(data)


def public_account(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = data if data is not None else _read_credential()
    if not data.get("token"):
        return {"connected": False, "login": "", "name": "", "avatar_url": "", "scopes": []}
    return {
        "connected": True,
        "login": str(data.get("login") or ""),
        "name": str(data.get("name") or ""),
        "avatar_url": str(data.get("avatar_url") or ""),
        "scopes": list(data.get("scopes") or []),
        "saved_at": str(data.get("saved_at") or ""),
    }


def disconnect() -> None:
    path = _credential_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def account_status() -> Dict[str, Any]:
    data = _read_credential()
    if not data.get("token"):
        return public_account(data)
    return public_account(data)


def list_repositories(limit: int = 100) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    response = _request(
        "GET",
        "/user/repos",
        params={"per_page": limit, "sort": "updated", "affiliation": "owner,collaborator,organization_member"},
    )
    repos = []
    for item in response.json():
        repos.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "full_name": item.get("full_name"),
                "private": bool(item.get("private")),
                "description": item.get("description") or "",
                "default_branch": item.get("default_branch") or "main",
                "clone_url": item.get("clone_url") or "",
                "html_url": item.get("html_url") or "",
                "updated_at": item.get("updated_at") or "",
                "language": item.get("language") or "",
            }
        )
    return repos


def create_repository(
    name: str,
    *,
    private: bool = True,
    description: str = "",
    auto_init: bool = True,
) -> Dict[str, Any]:
    name = str(name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name):
        raise GitHubError("仓库名称只能包含字母、数字、下划线、短横线和点")
    payload: Dict[str, Any] = {
        "name": name,
        "private": bool(private),
        "description": str(description or "")[:350],
        "auto_init": bool(auto_init),
    }
    response = _request("POST", "/user/repos", json_body=payload)
    item = response.json()
    return {
        "name": item.get("name"),
        "full_name": item.get("full_name"),
        "private": bool(item.get("private")),
        "default_branch": item.get("default_branch") or "main",
        "clone_url": item.get("clone_url") or "",
        "html_url": item.get("html_url") or "",
    }


def list_branches(repo: str, limit: int = 100) -> List[Dict[str, Any]]:
    repo = _validate_repo(repo)
    response = _request(
        "GET", f"/repos/{repo}/branches", params={"per_page": max(1, min(int(limit), 100))}
    )
    return [
        {
            "name": item.get("name"),
            "protected": bool(item.get("protected")),
            "sha": str((item.get("commit") or {}).get("sha") or ""),
        }
        for item in response.json()
    ]


def list_pulls(repo: str, state: str = "open", limit: int = 50) -> List[Dict[str, Any]]:
    repo = _validate_repo(repo)
    state = state if state in {"open", "closed", "all"} else "open"
    response = _request(
        "GET",
        f"/repos/{repo}/pulls",
        params={"state": state, "per_page": max(1, min(int(limit), 100))},
    )
    return [
        {
            "number": item.get("number"),
            "title": item.get("title") or "",
            "state": item.get("state") or "",
            "draft": bool(item.get("draft")),
            "user": (item.get("user") or {}).get("login") or "",
            "head": (item.get("head") or {}).get("ref") or "",
            "base": (item.get("base") or {}).get("ref") or "",
            "html_url": item.get("html_url") or "",
            "created_at": item.get("created_at") or "",
            "updated_at": item.get("updated_at") or "",
        }
        for item in response.json()
    ]


def create_pull(
    repo: str,
    *,
    title: str,
    head: str,
    base: str,
    body: str = "",
    draft: bool = False,
) -> Dict[str, Any]:
    repo = _validate_repo(repo)
    if not str(title or "").strip():
        raise GitHubError("PR 标题不能为空")
    if not str(head or "").strip() or not str(base or "").strip():
        raise GitHubError("PR 的 head/base 分支不能为空")
    response = _request(
        "POST",
        f"/repos/{repo}/pulls",
        json_body={
            "title": str(title).strip()[:250],
            "head": str(head).strip(),
            "base": str(base).strip(),
            "body": str(body or "")[:10000],
            "draft": bool(draft),
        },
    )
    item = response.json()
    return {
        "number": item.get("number"),
        "title": item.get("title") or "",
        "state": item.get("state") or "",
        "html_url": item.get("html_url") or "",
        "head": (item.get("head") or {}).get("ref") or "",
        "base": (item.get("base") or {}).get("ref") or "",
    }


def list_issues(repo: str, state: str = "open", limit: int = 50) -> List[Dict[str, Any]]:
    repo = _validate_repo(repo)
    state = state if state in {"open", "closed", "all"} else "open"
    response = _request(
        "GET",
        f"/repos/{repo}/issues",
        params={"state": state, "per_page": max(1, min(int(limit), 100))},
    )
    return [
        {
            "number": item.get("number"),
            "title": item.get("title") or "",
            "state": item.get("state") or "",
            "user": (item.get("user") or {}).get("login") or "",
            "labels": [str(label.get("name") or "") for label in item.get("labels") or []],
            "html_url": item.get("html_url") or "",
            "created_at": item.get("created_at") or "",
            "comments": int(item.get("comments") or 0),
        }
        for item in response.json()
        if "pull_request" not in item
    ]


def create_issue(repo: str, *, title: str, body: str = "") -> Dict[str, Any]:
    repo = _validate_repo(repo)
    if not str(title or "").strip():
        raise GitHubError("Issue 标题不能为空")
    response = _request(
        "POST",
        f"/repos/{repo}/issues",
        json_body={"title": str(title).strip()[:250], "body": str(body or "")[:20000]},
    )
    item = response.json()
    return {
        "number": item.get("number"),
        "title": item.get("title") or "",
        "state": item.get("state") or "",
        "html_url": item.get("html_url") or "",
    }
