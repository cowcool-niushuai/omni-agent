"""Admin API for managing local stdio MCP servers."""

from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import mcp_config
from mcp_manager import mcp_manager
from omni_agent import tenancy

router = APIRouter(prefix="/api/mcp", tags=["mcp-admin"])


class MCPServerPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    command: str = Field(..., min_length=1, max_length=2048)
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    description: str = ""
    enabled: bool = True


class MCPServerUpdate(BaseModel):
    command: Optional[str] = Field(default=None, max_length=2048)
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    cwd: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None


class MCPTestPayload(BaseModel):
    command: str = Field(..., min_length=1, max_length=2048)
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    cwd: str = ""


def _require_admin() -> None:
    context = tenancy.get_context()
    if context.role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以管理 MCP 服务")


@router.get("/config")
async def list_mcp_config() -> List[dict]:
    _require_admin()
    return mcp_config.list_configs(mcp_manager)


@router.post("/servers", status_code=201)
async def create_mcp_server(payload: MCPServerPayload) -> dict:
    _require_admin()
    try:
        return mcp_config.upsert(
            mcp_manager,
            payload.name,
            command=payload.command,
            args=payload.args,
            env=payload.env,
            cwd=payload.cwd,
            description=payload.description,
            enabled=payload.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"保存 MCP 配置失败: {exc}") from exc


@router.patch("/servers/{name}")
async def update_mcp_server(name: str, payload: MCPServerUpdate) -> dict:
    _require_admin()
    current = next(
        (item for item in mcp_config.list_configs(mcp_manager) if item["name"] == name),
        None,
    )
    if current is None:
        raise HTTPException(status_code=404, detail="MCP Server 不存在")
    values = {
        "command": current["command"],
        "args": current["args"],
        "env": current["env"],
        "cwd": current["cwd"],
        "description": current["description"],
        "enabled": current["enabled"],
    }
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            values[key] = value
    try:
        return mcp_config.upsert(mcp_manager, name, **values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"保存 MCP 配置失败: {exc}") from exc


@router.delete("/servers/{name}")
async def delete_mcp_server(name: str) -> dict:
    _require_admin()
    if not mcp_config.delete(mcp_manager, name):
        raise HTTPException(status_code=404, detail="MCP Server 不存在")
    return {"deleted": True, "name": name}


@router.post("/servers/test")
async def test_mcp_server(payload: MCPTestPayload) -> dict:
    _require_admin()
    try:
        return mcp_config.test_server(
            mcp_manager,
            command=payload.command,
            args=payload.args,
            env=payload.env,
            cwd=payload.cwd,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reload")
async def reload_mcp_servers() -> List[dict]:
    _require_admin()
    return mcp_config.reload(mcp_manager)
