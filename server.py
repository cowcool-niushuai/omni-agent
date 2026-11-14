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

import audit
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
        mcp_manager.stop_all()
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
        dirs[:] = [entry for entry in dirs if entry not in {".home", ".tmp"}]
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
    category, key, content = _validate_memory(body.category, body.key, body.content)
    return database.save_memory(category, key, content, body.importance)


@app.patch("/api/memories/{memory_id}")
async def api_update_memory(memory_id: int, body: MemoryUpdateRequest):
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
    title, content = _validate_knowledge(body.title, body.content)
    return database.add_knowledge(title, content, body.tags)


@app.patch("/api/knowledge/{doc_id}")
async def api_update_knowledge(doc_id: int, body: KnowledgeUpdateRequest):
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
    if not database.delete_knowledge(doc_id):
        raise HTTPException(status_code=404, detail="知识文档不存在")
    return {"deleted": True, "id": doc_id}

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


@app.get("/api/security/policy")
async def api_security_policy():
    from omni_agent.security.sandbox import detect_backend

    return {
        "sandbox_backend": detect_backend(settings.sandbox_backend),
        "sandbox_network": settings.sandbox_network,
        "command_policy_mode": settings.command_policy_mode,
        "command_timeout_seconds": settings.command_timeout_seconds,
        "tool_timeout_seconds": settings.tool_timeout_seconds,
        "tool_max_output_chars": settings.tool_max_output_chars,
        "max_file_write_bytes": settings.max_file_write_bytes,
        "web_fetch_allowed_ports": sorted(settings.web_fetch_port_list),
        "allow_private_network": settings.allow_private_network,
        "audit_enabled": settings.audit_enabled,
    }

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
