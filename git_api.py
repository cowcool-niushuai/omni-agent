"""Git workspace and GitHub management APIs."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

import git_service
import github_service
from omni_agent import tenancy

router = APIRouter(prefix="/api/git", tags=["git"])


def _workspace():
    return tenancy.current_workspace()


def _require_write() -> None:
    if not tenancy.get_context().can_write:
        raise HTTPException(status_code=403, detail="当前角色没有 Git 写权限")


def _git_error(exc: Exception) -> HTTPException:
    if isinstance(exc, git_service.GitError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, github_service.GitHubError):
        return HTTPException(status_code=exc.status if 400 <= exc.status < 600 else 502, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


class RepoRequest(BaseModel):
    repo: str = ""


class FilesRequest(RepoRequest):
    files: List[str] = Field(default_factory=list)


class CommitRequest(RepoRequest):
    message: str = Field(min_length=1, max_length=500)
    files: List[str] = Field(default_factory=list)


class CheckoutRequest(RepoRequest):
    branch: str = Field(min_length=1, max_length=200)
    create: bool = False


class RemoteRequest(RepoRequest):
    url: str = Field(min_length=1, max_length=2000)
    name: str = "origin"


class PushRequest(RepoRequest):
    branch: str = ""


class CloneRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    target: str = Field(min_length=1, max_length=200)
    token: bool = False


class GitHubConnectRequest(BaseModel):
    token: str = Field(min_length=1, max_length=500)


class GitHubRepoCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    private: bool = True
    description: str = ""
    auto_init: bool = True


class PullRequestCreate(BaseModel):
    repo: str = Field(min_length=3, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    head: str = Field(min_length=1, max_length=200)
    base: str = Field(min_length=1, max_length=200)
    body: str = ""
    draft: bool = False


class IssueCreateRequest(BaseModel):
    repo: str = Field(min_length=3, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    body: str = ""


# ---------------------------------------------------------------------------
# Git workspace
# ---------------------------------------------------------------------------


@router.get("/repos")
async def api_git_repos():
    return await run_in_threadpool(git_service.list_repos, _workspace())


@router.get("/status")
async def api_git_status(repo: str = ""):
    try:
        return await run_in_threadpool(git_service.status, _workspace(), repo)
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/init")
async def api_git_init(body: RepoRequest):
    _require_write()
    try:
        return await run_in_threadpool(git_service.init, _workspace(), body.repo)
    except Exception as exc:
        raise _git_error(exc) from exc


@router.get("/diff")
async def api_git_diff(repo: str = "", staged: bool = False):
    try:
        return await run_in_threadpool(git_service.diff, _workspace(), repo, staged=staged)
    except Exception as exc:
        raise _git_error(exc) from exc


@router.get("/log")
async def api_git_log(repo: str = "", limit: int = Query(default=20, ge=1, le=50)):
    try:
        return await run_in_threadpool(git_service.log, _workspace(), repo, limit=limit)
    except Exception as exc:
        raise _git_error(exc) from exc


@router.get("/branches")
async def api_git_branches(repo: str = ""):
    try:
        return await run_in_threadpool(git_service.branches, _workspace(), repo)
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/stage")
async def api_git_stage(body: FilesRequest):
    _require_write()
    try:
        return await run_in_threadpool(git_service.stage, _workspace(), body.repo, body.files)
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/unstage")
async def api_git_unstage(body: FilesRequest):
    _require_write()
    try:
        return await run_in_threadpool(
            git_service.unstage, _workspace(), body.repo, body.files
        )
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/commit")
async def api_git_commit(body: CommitRequest):
    _require_write()
    try:
        return await run_in_threadpool(
            git_service.commit,
            _workspace(),
            body.repo,
            message=body.message,
            files=body.files,
        )
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/checkout")
async def api_git_checkout(body: CheckoutRequest):
    _require_write()
    try:
        return await run_in_threadpool(
            git_service.checkout,
            _workspace(),
            body.repo,
            branch=body.branch,
            create=body.create,
        )
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/remote")
async def api_git_remote(body: RemoteRequest):
    _require_write()
    try:
        return await run_in_threadpool(
            git_service.set_remote,
            _workspace(),
            body.repo,
            name=body.name,
            url=body.url,
        )
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/push")
async def api_git_push(body: PushRequest):
    _require_write()
    token = await run_in_threadpool(github_service.load_token) or ""
    try:
        return await run_in_threadpool(
            git_service.push,
            _workspace(),
            body.repo,
            branch=body.branch,
            token=token,
        )
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/pull")
async def api_git_pull(body: PushRequest):
    _require_write()
    token = await run_in_threadpool(github_service.load_token) or ""
    try:
        return await run_in_threadpool(
            git_service.pull,
            _workspace(),
            body.repo,
            branch=body.branch,
            token=token,
        )
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/fetch")
async def api_git_fetch(body: RepoRequest):
    _require_write()
    token = await run_in_threadpool(github_service.load_token) or ""
    try:
        return await run_in_threadpool(
            git_service.fetch, _workspace(), body.repo, token=token
        )
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/clone")
async def api_git_clone(body: CloneRequest):
    _require_write()
    token = ""
    if body.token:
        token = await run_in_threadpool(github_service.load_token) or ""
    try:
        return await run_in_threadpool(
            git_service.clone,
            _workspace(),
            url=body.url,
            target=body.target,
            token=token,
        )
    except Exception as exc:
        raise _git_error(exc) from exc


# ---------------------------------------------------------------------------
# GitHub account and resources
# ---------------------------------------------------------------------------


@router.get("/github/account")
async def api_github_account():
    try:
        return await run_in_threadpool(github_service.account_status)
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/github/connect")
async def api_github_connect(body: GitHubConnectRequest):
    try:
        return await run_in_threadpool(github_service.connect, body.token)
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/github/disconnect")
async def api_github_disconnect():
    await run_in_threadpool(github_service.disconnect)
    return {"ok": True}


@router.get("/github/repos")
async def api_github_repos(limit: int = Query(default=100, ge=1, le=100)):
    try:
        return await run_in_threadpool(github_service.list_repositories, limit)
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/github/repos")
async def api_github_create_repo(body: GitHubRepoCreateRequest):
    try:
        return await run_in_threadpool(
            github_service.create_repository,
            body.name,
            private=body.private,
            description=body.description,
            auto_init=body.auto_init,
        )
    except Exception as exc:
        raise _git_error(exc) from exc


@router.get("/github/branches")
async def api_github_branches(repo: str = Query(min_length=3), limit: int = Query(default=100, ge=1, le=100)):
    try:
        return await run_in_threadpool(github_service.list_branches, repo, limit=limit)
    except Exception as exc:
        raise _git_error(exc) from exc


@router.get("/github/pulls")
async def api_github_pulls(
    repo: str = Query(min_length=3),
    state: str = Query(default="open", pattern="^(open|closed|all)$"),
):
    try:
        return await run_in_threadpool(github_service.list_pulls, repo, state=state)
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/github/pulls")
async def api_github_create_pull(body: PullRequestCreate):
    try:
        return await run_in_threadpool(
            github_service.create_pull,
            body.repo,
            title=body.title,
            head=body.head,
            base=body.base,
            body=body.body,
            draft=body.draft,
        )
    except Exception as exc:
        raise _git_error(exc) from exc


@router.get("/github/issues")
async def api_github_issues(
    repo: str = Query(min_length=3),
    state: str = Query(default="open", pattern="^(open|closed|all)$"),
):
    try:
        return await run_in_threadpool(github_service.list_issues, repo, state=state)
    except Exception as exc:
        raise _git_error(exc) from exc


@router.post("/github/issues")
async def api_github_create_issue(body: IssueCreateRequest):
    try:
        return await run_in_threadpool(
            github_service.create_issue,
            body.repo,
            title=body.title,
            body=body.body,
        )
    except Exception as exc:
        raise _git_error(exc) from exc
