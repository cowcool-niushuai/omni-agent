"""Request-scoped tenant context.

Tools and database helpers are synchronous and historically assumed one global
workspace.  M2 introduces teams and projects, so the active user/project is
carried with a :class:`~contextvars.ContextVar`.  ``asyncio.to_thread`` copies
the current context, which means the tool worker threads see the same project
workspace as the agent graph that spawned them.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from omni_agent.config import get_settings

DEFAULT_USER_ID = "usr_admin"
DEFAULT_TEAM_ID = "team_default"
DEFAULT_PROJECT_ID = "proj_default"

GLOBAL_ROLES = {"admin", "member", "viewer"}
TEAM_ROLES = {"owner", "admin", "member", "viewer"}


@dataclass(frozen=True)
class TenantContext:
    user_id: str = DEFAULT_USER_ID
    project_id: str = DEFAULT_PROJECT_ID
    team_id: str | None = DEFAULT_TEAM_ID
    workspace: Path | None = None
    role: str = "admin"
    team_role: str | None = "owner"
    scopes: frozenset[str] = frozenset({"*"})
    auth_type: str = "local"
    csrf_token: str | None = None
    permission_mode: str = "strict"

    @property
    def workspace_path(self) -> Path:
        return (self.workspace or project_workspace(self.project_id)).resolve()

    @property
    def is_full_access(self) -> bool:
        return self.permission_mode == "full_access"

    def has_scope(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_write(self) -> bool:
        return self.role != "viewer" and self.team_role != "viewer"


_current: ContextVar[TenantContext | None] = ContextVar("omni_tenant", default=None)


def project_workspace(project_id: str | None = None) -> Path:
    """Return the isolated workspace for a project.

    The legacy/default project keeps using ``workspace/`` so existing files do
    not move during the M2 migration.  Other projects live below
    ``data/project_workspaces/<project_id>``, deliberately outside the default
    workspace: otherwise the default project's file tools and sandbox could
    traverse into every other project's files.
    """
    settings = get_settings()
    pid = (project_id or DEFAULT_PROJECT_ID).strip() or DEFAULT_PROJECT_ID
    if pid == DEFAULT_PROJECT_ID:
        path = Path(settings.workspace_dir).resolve()
    else:
        safe = "".join(ch for ch in pid if ch.isalnum() or ch in {"-", "_"})[:80]
        path = (
            Path(settings.data_dir).resolve()
            / "project_workspaces"
            / (safe or DEFAULT_PROJECT_ID)
        )
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_context() -> TenantContext:
    return _current.get() or TenantContext()


def set_context(
    context: TenantContext | None = None,
    *,
    user_id: str = DEFAULT_USER_ID,
    project_id: str = DEFAULT_PROJECT_ID,
    team_id: str | None = DEFAULT_TEAM_ID,
    workspace: Path | None = None,
    role: str = "admin",
    team_role: str | None = "owner",
    scopes: Iterable[str] = ("*",),
    auth_type: str = "local",
    csrf_token: str | None = None,
    permission_mode: str = "strict",
) -> Token[TenantContext | None]:
    if context is None:
        context = TenantContext(
            user_id=user_id or DEFAULT_USER_ID,
            project_id=project_id or DEFAULT_PROJECT_ID,
            team_id=team_id,
            workspace=(workspace or project_workspace(project_id)).resolve(),
            role=role if role in GLOBAL_ROLES else "viewer",
            team_role=team_role,
            scopes=frozenset(scopes or ()),
            auth_type=auth_type,
            csrf_token=csrf_token,
            permission_mode=permission_mode if permission_mode in {"strict", "full_access"} else "strict",
        )
    return _current.set(context)


def reset_context(token: Token[TenantContext | None]) -> None:
    _current.reset(token)


def current_user_id(default: str = DEFAULT_USER_ID) -> str:
    context = _current.get()
    return context.user_id if context is not None else default


def current_project_id(default: str = DEFAULT_PROJECT_ID) -> str:
    context = _current.get()
    return context.project_id if context is not None else default


def current_workspace() -> Path:
    context = _current.get()
    if context is not None:
        return context.workspace_path
    return project_workspace(DEFAULT_PROJECT_ID)


def current_has_scope(scope: str) -> bool:
    context = _current.get()
    return context is None or context.has_scope(scope)


def current_permission_mode() -> str:
    context = _current.get()
    return context.permission_mode if context is not None else "strict"


def is_full_access() -> bool:
    return current_permission_mode() == "full_access"
