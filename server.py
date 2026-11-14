import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, List, Optional

import structlog
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import database
import permission
import skills_manager
from agent_core import agent_core
from mcp_manager import mcp_manager
from model_manager import model_manager
from omni_agent.config import get_settings
from omni_agent.logging_config import configure_logging
from omni_agent.security import PathSecurityError, safe_join

settings = get_settings()
configure_logging(settings)
database.init_db()
skills_manager.init_default_skills()

log = structlog.get_logger("omni.http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await agent_core.initialize()
    log.info("agent_runtime_ready")
    try:
        yield
    finally:
        await agent_core.close()
        log.info("agent_runtime_closed")


app = FastAPI(
    title="OmniAgent - Autonomous AI Agent Platform",
    version=settings.app_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)

WORKSPACE_ROOT = str(Path(settings.workspace_dir).resolve())
STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "static"))


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("http_request_failed")
        structlog.contextvars.clear_contextvars()
        raise

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    log.info("http_request", status=response.status_code, duration_ms=duration_ms)
    structlog.contextvars.clear_contextvars()
    return response


# 1. Health check
@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "omni-agent",
        "version": settings.app_version,
        "environment": settings.environment,
        "mcp_servers": len(mcp_manager.clients),
        "skills_count": len(skills_manager.list_skills()),
    }

# 2. Sessions API
class SessionCreate(BaseModel):
    title: Optional[str] = "新对话任务"

@app.post("/api/sessions")
async def api_create_session(body: SessionCreate):
    import uuid
    s_id = str(uuid.uuid4())[:12]
    database.create_session(s_id, body.title or "新对话任务")
    return {"id": s_id, "title": body.title}

@app.get("/api/sessions")
async def api_list_sessions():
    return database.list_sessions()

@app.get("/api/sessions/{session_id}/messages")
async def api_get_messages(session_id: str):
    return database.get_session_messages(session_id)

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
    return _sse_response(
        agent_core.run_react_stream(body.session_id, body.message)
    )


class ChatResumeRequest(BaseModel):
    session_id: str
    request_id: str
    action: str  # APPROVE | REJECT


@app.post("/api/chat/resume")
async def api_chat_resume(body: ChatResumeRequest):
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
    return mcp_manager.list_server_status()

@app.get("/api/mcp/tools")
async def api_mcp_tools():
    return mcp_manager.get_all_mcp_tools()

# 6. Workspace File Explorer & Viewer API
def _scan_workspace() -> list[dict]:
    items = []
    for root, dirs, files in os.walk(WORKSPACE_ROOT):
        rel_root = os.path.relpath(root, WORKSPACE_ROOT)
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
        safe_path = safe_join(Path(WORKSPACE_ROOT), path)
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
    action = body.action.upper()
    if action not in {"APPROVE", "REJECT"}:
        raise HTTPException(status_code=400, detail="action 必须是 APPROVE 或 REJECT")

    status = permission.get_request_status(body.request_id)
    if status == "NOT_FOUND":
        # The UI sends the batch id for one approval card. Resolve it to the
        # individual pending requests created by the approval node.
        if not body.session_id:
            raise HTTPException(status_code=404, detail="审批请求不存在")
        request_ids = permission.pending_request_ids(body.session_id)
        if not request_ids:
            raise HTTPException(status_code=404, detail="审批请求不存在")
    else:
        request_ids = [body.request_id]

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
    cursor.execute("SELECT * FROM permission_requests WHERE status = 'PENDING' ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

# 8. SubAgent Tasks API (DeerFlow Style)
@app.get("/api/subagents")
async def api_list_subagents(session_id: Optional[str] = None):
    return database.list_subagents(session_id)

# 9. Memory & Knowledge Base API
@app.get("/api/memories")
async def api_get_memories():
    return database.get_all_memories()

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
    model_manager.set_active_model(body.model_id)
    return {"status": "ok", "active": model_manager.get_active_model_info()}

class ProviderKeyRequest(BaseModel):
    provider_key: str
    api_key: str
    base_url: Optional[str] = None

@app.post("/api/models/key")
async def api_update_provider_key(body: ProviderKeyRequest):
    model_manager.update_provider_key(body.provider_key, body.api_key, body.base_url)
    return {"status": "ok", "provider": body.provider_key}

class CustomModelRequest(BaseModel):
    provider_name: str
    base_url: str
    api_key: str
    model_name: str
    label: str

@app.post("/api/models/custom")
async def api_add_custom_model(body: CustomModelRequest):
    m_id = model_manager.add_custom_model(
        body.provider_name, body.base_url, body.api_key, body.model_name, body.label
    )
    return {"status": "ok", "model_id": m_id, "active": model_manager.get_active_model_info()}

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
