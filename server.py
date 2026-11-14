import csv
import io
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, List, Optional

import structlog
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import audit
import database
import permission
import skills_manager
import usage
from agent_core import agent_core
from browser import browser_manager
from git_api import router as git_router
from mcp_api import router as mcp_admin_router
from mcp_manager import mcp_manager
from model_manager import model_manager
from omni_agent import tenancy
from omni_agent.config import get_settings
from omni_agent.logging_config import configure_logging
from omni_agent.security import PathSecurityError, safe_join
from scheduler import scheduler
from uploads import UploadError, save_attachment

settings = get_settings()
configure_logging(settings)
database.init_db()
skills_manager.init_default_skills()

APP_STARTED_AT = time.time()
log = structlog.get_logger("omni.http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await agent_core.initialize()
    await scheduler.start()
    log.info("agent_runtime_ready")
    try:
        yield
    finally:
        await scheduler.stop()
        await run_in_threadpool(browser_manager.stop)
        await agent_core.close()
        mcp_manager.stop_all()
        log.info("agent_runtime_closed")


app = FastAPI(
    title="OmniAgent - Autonomous AI Agent Platform",
    version=settings.app_version,
    lifespan=lifespan,
)

app.include_router(mcp_admin_router)
app.include_router(git_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        "X-Project-ID",
        "X-CSRF-Token",
        "X-API-Key",
    ],
)

STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "static"))
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


PUBLIC_API_PATHS = {
    "/health",
    "/api/auth/status",
    "/api/auth/login",
}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_LOGIN_WINDOWS: dict[str, list[float]] = {}


def _request_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.headers.get("X-API-Key", "") or authorization.strip()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def _rate_limited(key: str, *, limit: int, window: float) -> bool:
    now = time.monotonic()
    bucket = [item for item in _LOGIN_WINDOWS.get(key, []) if now - item < window]
    if len(bucket) >= limit:
        _LOGIN_WINDOWS[key] = bucket
        return True
    bucket.append(now)
    _LOGIN_WINDOWS[key] = bucket
    if len(_LOGIN_WINDOWS) > 10_000:
        _LOGIN_WINDOWS.clear()
    return False


def _locked_out(key: str, *, limit: int, window: float) -> bool:
    now = time.monotonic()
    bucket = [item for item in _LOGIN_WINDOWS.get(key, []) if now - item < window]
    _LOGIN_WINDOWS[key] = bucket
    return len(bucket) >= limit


def _local_user() -> dict:
    user = database.get_user(tenancy.DEFAULT_USER_ID)
    if user:
        return user
    return {
        "id": tenancy.DEFAULT_USER_ID,
        "username": "admin",
        "display_name": "本地管理员",
        "role": "admin",
        "status": "active",
    }


def _identity_from_cookie(request: Request) -> Optional[dict]:
    sid = request.cookies.get(settings.auth_cookie_name, "")
    row = database.get_auth_session(sid)
    if not row:
        return None
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "display_name": row.get("display_name") or row["username"],
        "role": row["role"],
        "scopes": frozenset({"*"}),
        "auth_type": "cookie",
        "csrf_token": row["csrf_token"],
        "session_id": sid,
    }


def _identity_from_token(request: Request) -> Optional[dict]:
    token = _request_token(request)
    if not token or not token.startswith("omni_"):
        return None
    row = database.authenticate_api_token(token)
    if not row:
        return None
    scopes = frozenset(
        part for part in str(row.get("scopes") or "").split(",") if part
    ) or frozenset({"*"})
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "display_name": row.get("display_name") or row["username"],
        "role": row["role"],
        "scopes": scopes,
        "auth_type": "token",
        "csrf_token": None,
        "session_id": row["id"],
    }


def _resolve_identity(request: Request) -> Optional[dict]:
    """Return the authenticated identity, or None in enforced-auth mode."""
    if settings.auth_required:
        return _identity_from_cookie(request) or _identity_from_token(request)
    if settings.api_token:
        provided = _request_token(request)
        if provided and secrets.compare_digest(provided, settings.api_token):
            user = _local_user()
            return {
                "user_id": user["id"],
                "username": user["username"],
                "display_name": user.get("display_name") or user["username"],
                "role": user.get("role") or "admin",
                "scopes": frozenset({"*"}),
                "auth_type": "token",
                "csrf_token": None,
                "session_id": None,
            }
        return None
    user = _local_user()
    return {
        "user_id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
        "role": user.get("role") or "admin",
        "scopes": frozenset({"*"}),
        "auth_type": "local",
        "csrf_token": None,
        "session_id": None,
    }


def _build_context(identity: dict, request: Request) -> tenancy.TenantContext:
    user_id = str(identity["user_id"])
    role = str(identity.get("role") or "member")
    project_id = request.headers.get("X-Project-ID", "").strip()
    access: Optional[dict] = None
    if project_id:
        access = database.get_project_access(project_id, user_id, role)
        if access is None:
            raise PermissionError("无权访问该项目")
    else:
        projects = database.list_projects_for_user(
            user_id, include_all=(role == "admin")
        )
        if role == "admin":
            preferred = next(
                (p for p in projects if p["id"] == tenancy.DEFAULT_PROJECT_ID), None
            )
            project_id = (preferred or (projects[0] if projects else None) or {}).get("id", "")
        elif projects:
            project_id = str(projects[0]["id"])
        if not project_id:
            raise PermissionError("当前用户没有可访问的项目")
        access = database.get_project_access(project_id, user_id, role)
    if access is not None:
        team_id = str(access.get("team_id") or tenancy.DEFAULT_TEAM_ID)
        effective_role = str(access.get("effective_role") or "member")
    else:
        team_id = tenancy.DEFAULT_TEAM_ID
        effective_role = "admin" if role == "admin" else "member"
    if role == "viewer":
        effective_role = "viewer"
    elif role == "admin":
        effective_role = "admin"
    return tenancy.TenantContext(
        user_id=user_id,
        project_id=project_id,
        team_id=team_id,
        workspace=tenancy.project_workspace(project_id),
        role=effective_role,
        team_role=str(access.get("team_role") if access else "member"),
        scopes=frozenset(identity.get("scopes") or {"*"}),
        auth_type=str(identity.get("auth_type") or "local"),
        csrf_token=identity.get("csrf_token"),
        permission_mode=database.get_project_setting(
            project_id, "permission_mode", "strict"
        ),
    )


def _context() -> tenancy.TenantContext:
    return tenancy.get_context()


def _require_admin() -> tenancy.TenantContext:
    ctx = _context()
    if ctx.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return ctx


def _require_write() -> tenancy.TenantContext:
    ctx = _context()
    if ctx.role == "viewer" or not ctx.can_write:
        raise HTTPException(status_code=403, detail="当前角色为只读，不能执行写操作")
    return ctx


def _require_scope(scope: str) -> tenancy.TenantContext:
    ctx = _context()
    if not ctx.has_scope(scope):
        raise HTTPException(status_code=403, detail=f"API Token 缺少权限: {scope}")
    return ctx


def _require_project_role(project_id: str, roles: set[str]) -> dict:
    ctx = _context()
    access = database.get_project_access(project_id, ctx.user_id, ctx.role)
    if access is None:
        raise HTTPException(status_code=403, detail="无权访问该项目")
    if ctx.role != "admin" and access.get("team_role") not in roles:
        raise HTTPException(status_code=403, detail="项目权限不足")
    return access


def _require_session(session_id: str) -> dict:
    session = database.get_session_tenant(session_id)
    ctx = _context()
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if ctx.role != "admin" and session.get("project_id") != ctx.project_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")
    return session


def _guard_chat_session(session_id: str) -> None:
    session = database.get_session_tenant(session_id)
    ctx = _context()
    if session and ctx.role != "admin" and session.get("project_id") != ctx.project_id:
        raise HTTPException(status_code=403, detail="无权访问该会话")


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )
    token = None
    start = time.perf_counter()
    try:
        if request.url.path.startswith("/api/") and request.url.path not in PUBLIC_API_PATHS:
            identity = _resolve_identity(request)
            if identity is None:
                log.warning("auth_rejected", path=request.url.path)
                return JSONResponse(
                    status_code=401,
                    content={"detail": "需要登录或有效的 API Token"},
                    headers={"X-Request-ID": request_id},
                )
            if (
                identity.get("auth_type") == "cookie"
                and request.method.upper() in _UNSAFE_METHODS
            ):
                csrf = request.headers.get("X-CSRF-Token", "")
                if not csrf or not secrets.compare_digest(
                    csrf, str(identity.get("csrf_token") or "")
                ):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "CSRF 校验失败，请刷新页面后重试"},
                        headers={"X-Request-ID": request_id},
                    )
            ctx = _build_context(identity, request)
            request.state.identity = identity
            request.state.tenant = ctx
            token = tenancy.set_context(ctx)
        response = await call_next(request)
    except PermissionError as exc:
        response = JSONResponse(
            status_code=403,
            content={"detail": str(exc)},
            headers={"X-Request-ID": request_id},
        )
    except Exception:
        log.exception("http_request_failed")
        raise
    finally:
        if token is not None:
            tenancy.reset_context(token)

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    log.info("http_request", status=response.status_code, duration_ms=duration_ms)
    structlog.contextvars.clear_contextvars()
    return response


# 1. Health check and authentication.
@app.get("/api/auth/status")
async def api_auth_status(request: Request):
    required = bool(settings.auth_required or settings.api_token)
    identity = _resolve_identity(request)
    authenticated = identity is not None
    user = None
    if identity:
        user = {
            "id": identity["user_id"],
            "username": identity["username"],
            "display_name": identity["display_name"],
            "role": identity["role"],
        }
    return {
        "required": required,
        "authenticated": authenticated,
        "mode": "team" if settings.auth_required else ("token" if settings.api_token else "local"),
        "user": user,
        "csrf_token": identity.get("csrf_token") if identity else None,
    }


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def api_auth_login(request: Request, body: LoginRequest):
    if not settings.auth_required:
        raise HTTPException(status_code=400, detail="当前为本地模式，无需登录")
    ip = _client_ip(request)
    username = body.username.strip()
    if _rate_limited(f"login:{ip}", limit=10, window=60):
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")
    if _locked_out(
        f"fail:{username.lower()}",
        limit=settings.auth_max_failed_attempts,
        window=settings.auth_lockout_seconds,
    ):
        raise HTTPException(status_code=429, detail="账号暂时锁定，请稍后再试")
    user = database.authenticate_user(username, body.password)
    if user is None:
        _rate_limited(
            f"fail:{username.lower()}",
            limit=settings.auth_max_failed_attempts,
            window=settings.auth_lockout_seconds,
        )
        log.warning("login_failed", ip=ip, username=body.username[:64])
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    _LOGIN_WINDOWS.pop(f"fail:{username.lower()}", None)
    auth_session = database.create_auth_session(
        user["id"],
        days=settings.auth_session_days,
        user_agent=request.headers.get("User-Agent", ""),
        ip=ip,
    )
    response = JSONResponse(
        {
            "user": user,
            "csrf_token": auth_session["csrf_token"],
            "expires_at": auth_session["expires_at"],
            "projects": database.list_projects_for_user(
                user["id"], include_all=(user.get("role") == "admin")
            ),
        }
    )
    response.set_cookie(
        settings.auth_cookie_name,
        auth_session["id"],
        max_age=max(1, settings.auth_session_days) * 86400,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    sid = request.cookies.get(settings.auth_cookie_name, "")
    if sid:
        database.delete_auth_session(sid)
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(settings.auth_cookie_name, path="/")
    return response


@app.get("/api/auth/me")
async def api_auth_me(request: Request):
    identity = getattr(request.state, "identity", None) or _resolve_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="未登录")
    return {
        "user": {
            "id": identity["user_id"],
            "username": identity["username"],
            "display_name": identity["display_name"],
            "role": identity["role"],
        },
        "csrf_token": identity.get("csrf_token"),
        "auth_type": identity.get("auth_type"),
        "projects": database.list_projects_for_user(
            identity["user_id"], include_all=(identity["role"] == "admin")
        ),
        "teams": database.list_teams_for_user(
            identity["user_id"], include_all=(identity["role"] == "admin")
        ),
    }


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/auth/password")
async def api_auth_change_password(request: Request, body: PasswordChangeRequest):
    identity = getattr(request.state, "identity", None)
    if identity is None:
        raise HTTPException(status_code=401, detail="未登录")
    user = database.authenticate_user(identity["username"], body.current_password)
    if user is None:
        raise HTTPException(status_code=400, detail="当前密码不正确")
    try:
        database.update_user(identity["user_id"], password=body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "updated"}


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "omni-agent",
        "version": settings.app_version,
        "environment": settings.environment,
        "mcp_servers": len(mcp_manager.clients),
        "skills_count": len(skills_manager.list_skills()),
        "scheduler_running": scheduler.running,
        "scheduled_task_active": scheduler.active_task_id,
        "browser_running": browser_manager.running,
    }

@app.get("/api/system/info")
async def api_system_info():
    stats = await run_in_threadpool(database.get_database_stats)
    checkpoint_path = Path(settings.data_dir) / "checkpoints.db"
    try:
        checkpoint_bytes = checkpoint_path.stat().st_size
    except OSError:
        checkpoint_bytes = 0
    return {
        "version": settings.app_version,
        "environment": settings.environment,
        "uptime_seconds": round(time.time() - APP_STARTED_AT, 1),
        "workspace_dir": str(tenancy.current_workspace()),
        "data_dir": str(settings.data_dir),
        "database": stats,
        "checkpoint_bytes": checkpoint_bytes,
        "active_model": model_manager.get_active_model_info(),
        "scheduler": {
            "running": scheduler.running,
            "active_task_id": scheduler.active_task_id,
        },
        "browser_running": browser_manager.running,
        "mcp_servers": mcp_manager.list_server_status(),
    }

# 2. Sessions API
class SessionCreate(BaseModel):
    title: Optional[str] = "新对话任务"

@app.post("/api/sessions")
async def api_create_session(body: SessionCreate):
    _require_write()
    _require_scope("sessions:write")
    s_id = str(uuid.uuid4())[:12]
    ctx = _context()
    session = database.create_session(
        s_id,
        body.title or "新对话任务",
        owner_id=ctx.user_id,
        project_id=ctx.project_id,
    )
    return session or {"id": s_id, "title": body.title, "project_id": ctx.project_id}

@app.get("/api/sessions")
async def api_list_sessions(
    q: Optional[str] = Query(None, max_length=120),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return database.list_sessions(query=q, limit=limit, offset=offset)

class SessionUpdateRequest(BaseModel):
    title: str

@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str):
    _require_session(session_id)
    return database.get_session(session_id)

@app.patch("/api/sessions/{session_id}")
async def api_rename_session(session_id: str, body: SessionUpdateRequest):
    _require_write()
    _require_scope("sessions:write")
    _require_session(session_id)
    try:
        session = database.rename_session(session_id, body.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session

@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str):
    _require_write()
    _require_scope("sessions:write")
    session = _require_session(session_id)
    if _context().role != "admin" and session.get("owner_id") != _context().user_id:
        raise HTTPException(status_code=403, detail="只能删除自己创建的会话")
    try:
        await agent_core.delete_thread(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not database.delete_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"deleted": True, "id": session_id}

@app.delete("/api/sessions/{session_id}/messages")
async def api_clear_session_messages(session_id: str):
    _require_write()
    _require_scope("sessions:write")
    session = _require_session(session_id)
    if _context().role != "admin" and session.get("owner_id") != _context().user_id:
        raise HTTPException(status_code=403, detail="只能清空自己创建的会话")
    try:
        await agent_core.delete_thread(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    database.clear_session_messages(session_id)
    return {"cleared": True, "id": session_id}

@app.get("/api/sessions/{session_id}/export")
async def api_export_session(session_id: str):
    _require_session(session_id)
    session = database.get_session(session_id)
    messages = database.get_session_messages(session_id)
    lines = [f"# {session.get('title') or session_id}", ""]
    lines.append(f"- Session: `{session_id}`")
    lines.append(f"- Created: {session.get('created_at') or ''}")
    lines.append("")
    for message in messages:
        role = str(message.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        label = "User" if role == "user" else "Assistant"
        lines.append(f"## {label}")
        lines.append("")
        lines.append(str(message.get("content") or ""))
        thought = str(message.get("thought") or "").strip()
        if thought:
            lines.append("")
            lines.append("<details><summary>Thinking</summary>")
            lines.append("")
            lines.append(thought)
            lines.append("")
            lines.append("</details>")
        lines.append("")
    content = "\n".join(lines)
    safe_id = "".join(
        char if char.isascii() and (char.isalnum() or char in {"-", "_"}) else "_"
        for char in session_id
    )[:60]
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="omniagent-{safe_id}.md"'
        },
    )

@app.get("/api/sessions/{session_id}/messages")
async def api_get_messages(session_id: str):
    _require_session(session_id)
    return database.get_session_messages(session_id)


@app.get("/api/sessions/{session_id}/plan")
async def api_get_session_plan(session_id: str):
    _require_session(session_id)
    return await agent_core.get_plan(session_id)


@app.post("/api/sessions/{session_id}/compact")
async def api_compact_session(session_id: str):
    _require_session(session_id)
    _require_write()
    return await agent_core.compact_session(session_id)


class UploadRequest(BaseModel):
    filename: str
    content_base64: str
    session_id: str = ""


@app.post("/api/uploads")
async def api_upload_attachment(body: UploadRequest):
    _require_write()
    _require_scope("chat:write")
    try:
        return await run_in_threadpool(
            save_attachment,
            tenancy.current_workspace(),
            body.filename,
            body.content_base64,
            session_id=body.session_id,
            max_bytes=settings.attachment_max_bytes,
        )
    except UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

# 3. Realtime SSE Streaming Chat Endpoint
class ChatRequest(BaseModel):
    session_id: str
    message: str


async def _sse_events(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    try:
        async for event in events:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    except Exception as exc:
        log.exception("sse_stream_failed")
        err = {"type": "error", "message": f"Server stream error: {exc}"}
        yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"


def _sse_response(events: AsyncIterator[dict]) -> StreamingResponse:
    return StreamingResponse(
        _sse_events(events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chat/stream")
async def api_chat_stream(body: ChatRequest):
    _require_write()
    _require_scope("chat:write")
    _guard_chat_session(body.session_id)
    ctx = _context()
    return _sse_response(
        agent_core.run_react_stream(body.session_id, body.message, tenant=ctx)
    )


class ChatResumeRequest(BaseModel):
    session_id: str
    request_id: str
    action: str  # APPROVE | REJECT


@app.post("/api/chat/resume")
async def api_chat_resume(body: ChatResumeRequest):
    _require_write()
    _require_scope("chat:write")
    _require_session(body.session_id)
    action = body.action.upper()
    if action not in {"APPROVE", "REJECT"}:
        raise HTTPException(status_code=400, detail="action must be APPROVE or REJECT")
    return _sse_response(
        agent_core.resume_stream(body.session_id, body.request_id, action)
    )

# 4. Skills API
@app.get("/api/skills")
async def api_list_skills():
    return skills_manager.list_skills()

class SkillCreate(BaseModel):
    skill_id: str
    name: str
    description: str
    content: str
    tags: List[str] = []

@app.post("/api/skills")
async def api_create_skill(body: SkillCreate):
    _require_admin()
    try:
        skills_manager.save_skill(
            skill_id=body.skill_id,
            name=body.name,
            description=body.description,
            content=body.content,
            tags=body.tags,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"status": "created", "skill_id": body.skill_id}

# 5. MCP (Model Context Protocol) API
@app.get("/api/mcp/servers")
async def api_mcp_servers():
    return await run_in_threadpool(mcp_manager.list_server_status)

@app.get("/api/mcp/tools")
async def api_mcp_tools():
    return await run_in_threadpool(mcp_manager.get_all_mcp_tools)

# 6. Workspace File Explorer & Viewer API
def _scan_workspace() -> list[dict]:
    items = []
    workspace = tenancy.current_workspace().resolve()
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [entry for entry in dirs if entry not in {".home", ".tmp", ".uploads"}]
        rel_root = os.path.relpath(root, workspace)
        prefix = "" if rel_root == "." else rel_root + "/"
        for d in dirs:
            items.append({"type": "dir", "path": f"{prefix}{d}", "name": d})
        for f in files:
            full_p = os.path.join(root, f)
            size = os.path.getsize(full_p)
            ext = os.path.splitext(f)[1].lower()
            items.append(
                {
                    "type": "file",
                    "path": f"{prefix}{f}",
                    "name": f,
                    "size": size,
                    "extension": ext,
                }
            )
    return items


def _read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


@app.get("/api/workspace")
async def api_workspace_tree():
    return await run_in_threadpool(_scan_workspace)


@app.get("/api/workspace/file")
async def api_workspace_file_content(path: str = Query(...)):
    try:
        safe_path = safe_join(tenancy.current_workspace(), path)
    except PathSecurityError:
        raise HTTPException(status_code=403, detail="越权路径访问") from None
    if not safe_path.exists() or safe_path.is_dir():
        raise HTTPException(status_code=404, detail="文件不存在")
    try:
        content = await run_in_threadpool(_read_text_file, safe_path)
        return {"path": path, "content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from None

# 7. Permission & Human-in-the-loop API
class PermissionDecision(BaseModel):
    request_id: str
    action: str # "APPROVE" or "REJECT"
    session_id: Optional[str] = None

@app.post("/api/permission/confirm")
async def api_permission_confirm(body: PermissionDecision):
    ctx = _require_write()
    action = body.action.upper()
    if action not in {"APPROVE", "REJECT"}:
        raise HTTPException(status_code=400, detail="action 必须是 APPROVE 或 REJECT")

    if body.session_id:
        _require_session(body.session_id)
    request = permission.get_request(body.request_id)
    if request:
        if request.get("project_id") != ctx.project_id:
            raise HTTPException(status_code=404, detail="审批请求不存在")
        if body.session_id and request.get("session_id") != body.session_id:
            raise HTTPException(status_code=400, detail="审批请求与会话不匹配")
        request_ids = [body.request_id]
    else:
        # The UI sends the batch id for one approval card. Resolve it to the
        # individual pending requests created by the approval node.
        if not body.session_id:
            raise HTTPException(status_code=404, detail="审批请求不存在")
        request_ids = permission.pending_request_ids(
            body.session_id, project_id=ctx.project_id
        )
        if not request_ids:
            raise HTTPException(status_code=404, detail="审批请求不存在")

    if action == "APPROVE":
        permission.approve_requests(request_ids)
        return {
            "status": "APPROVED",
            "request_id": body.request_id,
            "request_ids": request_ids,
        }
    permission.reject_requests(request_ids)
    return {
        "status": "REJECTED",
        "request_id": body.request_id,
        "request_ids": request_ids,
    }

@app.get("/api/permission/pending")
async def api_permission_pending():
    conn = database.get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM permission_requests WHERE status = 'PENDING' AND project_id = ? "
        "ORDER BY created_at DESC",
        (_context().project_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# 8. SubAgent Tasks API (DeerFlow Style)
@app.get("/api/subagents")
async def api_list_subagents(session_id: Optional[str] = None):
    project_id = _context().project_id
    if session_id:
        project_id = _require_session(session_id).get("project_id") or project_id
    return database.list_subagents(session_id, project_id=project_id)

# 9. Memory & Knowledge Base API
class MemoryUpsertRequest(BaseModel):
    category: str = "fact"
    key: str
    content: str
    importance: float = 1.0


class MemoryUpdateRequest(BaseModel):
    category: Optional[str] = None
    key: Optional[str] = None
    content: Optional[str] = None
    importance: Optional[float] = None


class KnowledgeUpsertRequest(BaseModel):
    title: str
    content: str
    tags: str = ""


class KnowledgeUpdateRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[str] = None


def _validate_memory(category: str, key: str, content: str) -> tuple[str, str, str]:
    category = (category or "fact").strip()[:40]
    key = key.strip()
    content = content.strip()
    if not key or not content:
        raise HTTPException(status_code=400, detail="记忆的 key 和 content 不能为空")
    if len(key) > 120:
        raise HTTPException(status_code=400, detail="记忆键名过长（上限 120 字符）")
    if len(content) > settings.memory_max_content_chars:
        raise HTTPException(
            status_code=400,
            detail=f"记忆内容过长（上限 {settings.memory_max_content_chars} 字符）",
        )
    return category or "fact", key, content


def _validate_knowledge(title: str, content: str) -> tuple[str, str]:
    title = title.strip()
    content = content.strip()
    if not title or not content:
        raise HTTPException(status_code=400, detail="知识文档的 title 和 content 不能为空")
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="知识文档标题过长（上限 200 字符）")
    if len(content) > settings.knowledge_max_content_chars:
        raise HTTPException(
            status_code=400,
            detail=f"知识文档过长（上限 {settings.knowledge_max_content_chars} 字符）",
        )
    return title, content


@app.get("/api/memories")
async def api_get_memories(
    q: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    if q and q.strip():
        return database.search_memories(q, min(limit, 50))
    return database.get_all_memories(limit)


@app.post("/api/memories")
async def api_create_memory(body: MemoryUpsertRequest):
    _require_write()
    category, key, content = _validate_memory(body.category, body.key, body.content)
    return database.save_memory(category, key, content, body.importance)


@app.patch("/api/memories/{memory_id}")
async def api_update_memory(memory_id: int, body: MemoryUpdateRequest):
    _require_write()
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        memory = database.get_memory(memory_id)
        if not memory:
            raise HTTPException(status_code=404, detail="记忆不存在")
        return memory
    if "category" in updates and updates["category"] is not None:
        updates["category"] = str(updates["category"]).strip()[:40] or "fact"
    if "key" in updates and updates["key"] is not None:
        key = str(updates["key"]).strip()
        if not key:
            raise HTTPException(status_code=400, detail="记忆的 key 不能为空")
        if len(key) > 120:
            raise HTTPException(status_code=400, detail="记忆键名过长（上限 120 字符）")
        updates["key"] = key
    if "content" in updates and updates["content"] is not None:
        content = str(updates["content"]).strip()
        if not content:
            raise HTTPException(status_code=400, detail="记忆的 content 不能为空")
        if len(content) > settings.memory_max_content_chars:
            raise HTTPException(
                status_code=400,
                detail=f"记忆内容过长（上限 {settings.memory_max_content_chars} 字符）",
            )
        updates["content"] = content
    memory = database.update_memory(memory_id, **updates)
    if not memory:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return memory


@app.delete("/api/memories/{memory_id}")
async def api_delete_memory(memory_id: int):
    _require_write()
    if not database.delete_memory(memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"deleted": True, "id": memory_id}


@app.get("/api/knowledge")
async def api_get_knowledge(
    q: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    if q and q.strip():
        return database.search_knowledge(q, limit)
    return database.list_knowledge(limit, offset)


@app.post("/api/knowledge")
async def api_create_knowledge(body: KnowledgeUpsertRequest):
    _require_write()
    title, content = _validate_knowledge(body.title, body.content)
    return database.add_knowledge(title, content, body.tags)


@app.patch("/api/knowledge/{doc_id}")
async def api_update_knowledge(doc_id: int, body: KnowledgeUpdateRequest):
    _require_write()
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        doc = database.get_knowledge(doc_id)
        if not doc:
            raise HTTPException(status_code=404, detail="知识文档不存在")
        return doc
    if "title" in updates and updates["title"] is not None:
        title = str(updates["title"]).strip()
        if not title:
            raise HTTPException(status_code=400, detail="知识文档的 title 不能为空")
        if len(title) > 200:
            raise HTTPException(status_code=400, detail="知识文档标题过长（上限 200 字符）")
        updates["title"] = title
    if "content" in updates and updates["content"] is not None:
        content = str(updates["content"]).strip()
        if not content:
            raise HTTPException(status_code=400, detail="知识文档的 content 不能为空")
        if len(content) > settings.knowledge_max_content_chars:
            raise HTTPException(
                status_code=400,
                detail=f"知识文档过长（上限 {settings.knowledge_max_content_chars} 字符）",
            )
        updates["content"] = content
    doc = database.update_knowledge(doc_id, **updates)
    if not doc:
        raise HTTPException(status_code=404, detail="知识文档不存在")
    return doc


@app.delete("/api/knowledge/{doc_id}")
async def api_delete_knowledge(doc_id: int):
    _require_write()
    if not database.delete_knowledge(doc_id):
        raise HTTPException(status_code=404, detail="知识文档不存在")
    return {"deleted": True, "id": doc_id}

# 9c. Scheduled task API (Phase 3)
class ScheduleCreateRequest(BaseModel):
    name: str
    prompt: str
    interval_seconds: int = 3600
    enabled: bool = True


class ScheduleUpdateRequest(BaseModel):
    name: Optional[str] = None
    prompt: Optional[str] = None
    interval_seconds: Optional[int] = None
    enabled: Optional[bool] = None


def _validate_schedule(name: str, prompt: str, interval_seconds: int) -> tuple[str, str, int]:
    name = name.strip()
    prompt = prompt.strip()
    if not name or not prompt:
        raise HTTPException(status_code=400, detail="定时任务的名称和提示词不能为空")
    if len(name) > settings.scheduler_name_max_chars:
        raise HTTPException(
            status_code=400,
            detail=f"定时任务名称过长（上限 {settings.scheduler_name_max_chars} 字符）",
        )
    if len(prompt) > settings.scheduler_prompt_max_chars:
        raise HTTPException(
            status_code=400,
            detail=f"定时任务提示词过长（上限 {settings.scheduler_prompt_max_chars} 字符）",
        )
    if not (
        settings.scheduler_min_interval_seconds
        <= int(interval_seconds)
        <= settings.scheduler_max_interval_seconds
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "定时间隔必须在 "
                f"{settings.scheduler_min_interval_seconds} 到 "
                f"{settings.scheduler_max_interval_seconds} 秒之间"
            ),
        )
    return name, prompt, int(interval_seconds)


@app.get("/api/schedules")
async def api_list_schedules():
    return database.list_scheduled_tasks()


@app.post("/api/schedules")
async def api_create_schedule(body: ScheduleCreateRequest):
    _require_write()
    existing = database.list_scheduled_tasks()
    if len(existing) >= settings.scheduler_max_tasks:
        raise HTTPException(
            status_code=400,
            detail=f"定时任务数量已达上限（{settings.scheduler_max_tasks}）",
        )
    name, prompt, interval = _validate_schedule(
        body.name, body.prompt, body.interval_seconds
    )
    try:
        return database.create_scheduled_task(name, prompt, interval, body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.get("/api/schedules/{task_id}")
async def api_get_schedule(task_id: str):
    task = database.get_scheduled_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return task


@app.patch("/api/schedules/{task_id}")
async def api_update_schedule(task_id: str, body: ScheduleUpdateRequest):
    _require_write()
    current = database.get_scheduled_task(task_id)
    if not current:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    updates = {
        key: value
        for key, value in body.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if not updates:
        return current
    if "name" in updates:
        updates["name"] = str(updates["name"]).strip()
        if not updates["name"]:
            raise HTTPException(status_code=400, detail="定时任务的名称不能为空")
        if len(updates["name"]) > settings.scheduler_name_max_chars:
            raise HTTPException(
                status_code=400,
                detail=f"定时任务名称过长（上限 {settings.scheduler_name_max_chars} 字符）",
            )
    if "prompt" in updates:
        updates["prompt"] = str(updates["prompt"]).strip()
        if not updates["prompt"]:
            raise HTTPException(status_code=400, detail="定时任务的提示词不能为空")
        if len(updates["prompt"]) > settings.scheduler_prompt_max_chars:
            raise HTTPException(
                status_code=400,
                detail=f"定时任务提示词过长（上限 {settings.scheduler_prompt_max_chars} 字符）",
            )
    if "interval_seconds" in updates:
        interval = int(updates["interval_seconds"])
        if not (
            settings.scheduler_min_interval_seconds
            <= interval
            <= settings.scheduler_max_interval_seconds
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "定时间隔必须在 "
                    f"{settings.scheduler_min_interval_seconds} 到 "
                    f"{settings.scheduler_max_interval_seconds} 秒之间"
                ),
            )
        updates["interval_seconds"] = interval
    try:
        updated = database.update_scheduled_task(task_id, **updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not updated:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return updated


@app.delete("/api/schedules/{task_id}")
async def api_delete_schedule(task_id: str):
    _require_write()
    if not database.delete_scheduled_task(task_id):
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return {"deleted": True, "id": task_id}


@app.post("/api/schedules/{task_id}/run")
async def api_run_schedule(task_id: str):
    _require_write()
    task = database.get_scheduled_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    if not scheduler.trigger(task_id):
        raise HTTPException(status_code=409, detail="已有定时任务正在运行，请稍后再试")
    return {"started": True, "task_id": task_id}


# 9b. Audit & security policy API (Phase 2)
@app.get("/api/audit")
async def api_audit(
    session_id: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return audit.list_tool_calls(session_id=session_id, limit=limit, offset=offset)


@app.get("/api/audit/stats")
async def api_audit_stats(session_id: Optional[str] = None):
    return audit.audit_stats(session_id)


@app.get("/api/usage/stats")
async def api_usage_stats(session_id: Optional[str] = None):
    if session_id:
        _require_session(session_id)
    return await run_in_threadpool(usage.usage_summary, session_id)


@app.get("/api/browser/status")
async def api_browser_status():
    return await run_in_threadpool(browser_manager.status)


class PermissionModeRequest(BaseModel):
    permission_mode: str


def _security_policy_payload(mode: str) -> dict:
    from omni_agent.security.sandbox import detect_backend

    full_access = mode == "full_access"
    return {
        "permission_mode": mode,
        "sandbox_backend": "host" if full_access else detect_backend(settings.sandbox_backend),
        "sandbox_network": True if full_access else settings.sandbox_network,
        "command_policy_mode": "full_access" if full_access else settings.command_policy_mode,
        "command_timeout_seconds": settings.command_timeout_seconds,
        "tool_timeout_seconds": settings.tool_timeout_seconds,
        "tool_max_output_chars": settings.tool_max_output_chars,
        "max_file_write_bytes": settings.max_file_write_bytes,
        "web_fetch_allowed_ports": sorted(settings.web_fetch_port_list),
        "allow_private_network": settings.allow_private_network,
        "audit_enabled": settings.audit_enabled,
    }


@app.get("/api/security/policy")
async def api_security_policy():
    return _security_policy_payload(_context().permission_mode)


@app.patch("/api/security/policy")
async def api_update_security_policy(body: PermissionModeRequest):
    context = _require_admin()
    mode = body.permission_mode.strip()
    if mode not in {"strict", "full_access"}:
        raise HTTPException(status_code=400, detail="权限模式只能是 strict 或 full_access")
    database.set_project_setting(context.project_id, "permission_mode", mode)
    log.info(
        "permission_mode_changed",
        project_id=context.project_id,
        user_id=context.user_id,
        permission_mode=mode,
    )
    return _security_policy_payload(mode)

# 9d. Team edition: users, teams, projects and API tokens
class UserCreateRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    email: str = ""
    role: str = "member"


class UserUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    status: Optional[str] = None
    password: Optional[str] = None


class TeamCreateRequest(BaseModel):
    name: str
    description: str = ""
    owner_id: Optional[str] = None


class TeamUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None


class TeamMemberRequest(BaseModel):
    user_id: str
    role: str = "member"


class TeamMemberUpdateRequest(BaseModel):
    role: str


class ProjectCreateRequest(BaseModel):
    team_id: Optional[str] = None
    name: str
    description: str = ""


class ProjectUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None


class TokenCreateRequest(BaseModel):
    name: str
    scopes: List[str] = []
    expires_days: Optional[int] = None


def _require_team_admin(team_id: str) -> None:
    ctx = _context()
    if ctx.role == "admin":
        return
    member = database.get_team_member(team_id, ctx.user_id)
    if not member or member.get("role") not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="需要团队管理员权限")


@app.get("/api/users")
async def api_list_users(q: Optional[str] = None):
    _require_admin()
    return database.list_users(q)


@app.post("/api/users")
async def api_create_user(body: UserCreateRequest):
    _require_admin()
    try:
        return database.create_user(
            body.username,
            body.password,
            display_name=body.display_name,
            email=body.email,
            role=body.role,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/users/{user_id}")
async def api_update_user(user_id: str, body: UserUpdateRequest):
    ctx = _require_admin()
    updates = {key: value for key, value in body.model_dump(exclude_unset=True).items() if value is not None}
    if not updates:
        user = database.get_user(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="用户不存在")
        return user
    current = database.get_user(user_id)
    if not current:
        raise HTTPException(status_code=404, detail="用户不存在")
    if current.get("role") == "admin" and (
        updates.get("role") not in (None, "admin") or updates.get("status") == "disabled"
    ):
        if database.count_active_admins() <= 1:
            raise HTTPException(status_code=400, detail="不能移除最后一个可用的管理员")
    if user_id == ctx.user_id and updates.get("status") == "disabled":
        raise HTTPException(status_code=400, detail="不能禁用当前登录账号")
    try:
        return database.update_user(user_id, **updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/teams")
async def api_list_teams():
    ctx = _context()
    return database.list_teams_for_user(ctx.user_id, include_all=(ctx.role == "admin"))


@app.post("/api/teams")
async def api_create_team(body: TeamCreateRequest):
    _require_admin()
    owner_id = body.owner_id or _context().user_id
    if not database.get_user(owner_id):
        raise HTTPException(status_code=400, detail="负责人不存在")
    try:
        return database.create_team(body.name, owner_id, description=body.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/teams/{team_id}")
async def api_update_team(team_id: str, body: TeamUpdateRequest):
    if not database.get_team(team_id):
        raise HTTPException(status_code=404, detail="团队不存在")
    _require_team_admin(team_id)
    try:
        return database.update_team(
            team_id,
            name=body.name,
            description=body.description,
            status=body.status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/teams/{team_id}/members")
async def api_list_team_members(team_id: str):
    if not database.get_team(team_id):
        raise HTTPException(status_code=404, detail="团队不存在")
    _require_team_admin(team_id)
    return database.list_team_members(team_id)


@app.post("/api/teams/{team_id}/members")
async def api_add_team_member(team_id: str, body: TeamMemberRequest):
    if not database.get_team(team_id):
        raise HTTPException(status_code=404, detail="团队不存在")
    _require_team_admin(team_id)
    try:
        return database.add_team_member(team_id, body.user_id, body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/teams/{team_id}/members/{user_id}")
async def api_update_team_member(team_id: str, user_id: str, body: TeamMemberUpdateRequest):
    if not database.get_team(team_id):
        raise HTTPException(status_code=404, detail="团队不存在")
    _require_team_admin(team_id)
    try:
        member = database.update_team_member(team_id, user_id, body.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not member:
        raise HTTPException(status_code=404, detail="团队成员不存在")
    return member


@app.delete("/api/teams/{team_id}/members/{user_id}")
async def api_remove_team_member(team_id: str, user_id: str):
    if not database.get_team(team_id):
        raise HTTPException(status_code=404, detail="团队不存在")
    _require_team_admin(team_id)
    if not database.remove_team_member(team_id, user_id):
        raise HTTPException(status_code=404, detail="团队成员不存在")
    return {"deleted": True, "team_id": team_id, "user_id": user_id}


@app.get("/api/projects")
async def api_list_projects(include_archived: bool = False):
    ctx = _context()
    return database.list_projects_for_user(
        ctx.user_id,
        include_all=(ctx.role == "admin"),
        include_archived=include_archived,
    )


@app.post("/api/projects")
async def api_create_project(body: ProjectCreateRequest):
    ctx = _context()
    team_id = body.team_id or ctx.team_id or tenancy.DEFAULT_TEAM_ID
    if not database.get_team(team_id):
        raise HTTPException(status_code=400, detail="团队不存在")
    _require_team_admin(team_id)
    try:
        return database.create_project(
            team_id,
            body.name,
            ctx.user_id,
            description=body.description,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/projects/{project_id}")
async def api_update_project(project_id: str, body: ProjectUpdateRequest):
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    _require_team_admin(str(project["team_id"]))
    try:
        return database.update_project(
            project_id,
            name=body.name,
            description=body.description,
            status=body.status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/projects/{project_id}")
async def api_archive_project(project_id: str):
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    if project_id == tenancy.DEFAULT_PROJECT_ID:
        raise HTTPException(status_code=400, detail="默认项目不能归档")
    _require_team_admin(str(project["team_id"]))
    return database.update_project(project_id, status="archived")


@app.get("/api/tokens")
async def api_list_tokens():
    return database.list_api_tokens(_context().user_id)


@app.post("/api/tokens")
async def api_create_token(body: TokenCreateRequest):
    _require_write()
    scopes = [item.strip() for item in body.scopes if item.strip()] or ["*"]
    try:
        return database.create_api_token(
            _context().user_id,
            body.name,
            scopes=scopes,
            expires_days=body.expires_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/tokens/{token_id}")
async def api_revoke_token(token_id: str):
    if not database.revoke_api_token(token_id, _context().user_id):
        raise HTTPException(status_code=404, detail="Token 不存在或已撤销")
    return {"revoked": True, "id": token_id}


@app.get("/api/audit/export")
async def api_audit_export(session_id: Optional[str] = None):
    rows = await run_in_threadpool(
        audit.export_tool_calls,
        project_id=_context().project_id,
    )
    if session_id:
        rows = [row for row in rows if row.get("session_id") == session_id]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "created_at",
            "session_id",
            "tool_name",
            "risk_level",
            "decision",
            "status",
            "sandbox",
            "duration_ms",
            "output_preview",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.get("id"),
                row.get("created_at"),
                row.get("session_id"),
                row.get("tool_name"),
                row.get("risk_level"),
                row.get("decision"),
                row.get("status"),
                row.get("sandbox"),
                row.get("duration_ms"),
                row.get("output_preview"),
            ]
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="omniagent-audit.csv"'},
    )


@app.post("/api/audit/purge")
async def api_audit_purge(days: Optional[int] = None):
    _require_admin()
    deleted = audit.purge_audit(days or settings.audit_retention_days)
    return {"deleted": deleted, "days": days or settings.audit_retention_days}


@app.get("/api/projects/{project_id}/stats")
async def api_project_stats(project_id: str):
    _require_project_role(project_id, {"owner", "admin", "member", "viewer"})
    return database.list_project_sessions_stats(project_id)


# 10. Multi-Provider & Model Management API
@app.get("/api/models")
async def api_get_models():
    model_manager.load_config()
    return {
        "active": model_manager.get_active_model_info(),
        "models": model_manager.get_all_models()
    }

class ActiveModelRequest(BaseModel):
    model_id: str

@app.post("/api/models/active")
async def api_set_active_model(body: ActiveModelRequest):
    _require_admin()
    try:
        model_manager.set_active_model(body.model_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "active": model_manager.get_active_model_info()}

class ProviderKeyRequest(BaseModel):
    provider_key: str
    api_key: str = ""
    base_url: Optional[str] = None
    adapter: Optional[str] = None
    env: Optional[List[str]] = None
    headers: Optional[dict[str, str]] = None

@app.post("/api/models/key")
async def api_update_provider_key(body: ProviderKeyRequest):
    _require_admin()
    try:
        model_manager.update_provider_key(
            body.provider_key,
            body.api_key,
            body.base_url,
            adapter=body.adapter,
            env=body.env,
            headers=body.headers,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "provider": body.provider_key}

class CustomModelRequest(BaseModel):
    provider_name: str
    base_url: str
    api_key: str = ""
    model_name: str
    label: str
    adapter: str = "openai-compatible"
    provider_key: Optional[str] = None
    model_id: Optional[str] = None
    capabilities: Optional[dict] = None
    limit: Optional[dict] = None
    env_name: Optional[str] = None

@app.post("/api/models/custom")
async def api_add_custom_model(body: CustomModelRequest):
    _require_admin()
    try:
        m_id = model_manager.add_custom_model(
            body.provider_name,
            body.base_url,
            body.api_key,
            body.model_name,
            body.label,
            adapter=body.adapter,
            provider_key=body.provider_key,
            model_id=body.model_id,
            capabilities=body.capabilities,
            limit=body.limit,
            env_name=body.env_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "model_id": m_id, "active": model_manager.get_active_model_info()}

class ModelTestRequest(BaseModel):
    provider_key: str
    base_url: Optional[str] = None
    adapter: Optional[str] = None
    api_key: Optional[str] = None
    model_name: Optional[str] = None

@app.post("/api/models/test")
async def api_test_model_provider(body: ModelTestRequest):
    _require_admin()
    return await model_manager.test_provider(
        body.provider_key,
        base_url=body.base_url,
        adapter=body.adapter,
        api_key=body.api_key,
        model_name=body.model_name,
    )

class ModelDiscoverRequest(BaseModel):
    provider_key: str

@app.post("/api/models/discover")
async def api_discover_models(body: ModelDiscoverRequest):
    _require_admin()
    try:
        added = await model_manager.discover_models(body.provider_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "status": "ok",
        "provider": body.provider_key,
        "added": added,
        "models": model_manager.get_all_models(),
    }

# 11. Serve Static UI
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = Path(STATIC_DIR) / "index.html"
    if await run_in_threadpool(index_path.exists):
        return FileResponse(index_path)
    return HTMLResponse("<h1>OmniAgent UI loading...</h1>")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host=settings.host, port=settings.port, reload=False)
