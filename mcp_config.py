"""Management helpers for local stdio MCP servers.

The MCP runtime lives in :mod:`mcp_manager`; this module only owns the
user-editable ``mcp_config.json`` file.  Keeping persistence separate makes it
possible to validate and test a server before it enters the live client set.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp_manager import MCP_CONFIG_FILE, MCPClient
from omni_agent.config import get_settings


def read_config() -> Dict[str, Any]:
    """Read the on-disk config, preserving disabled server entries."""
    try:
        with open(MCP_CONFIG_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    data["mcpServers"] = {
        str(name): dict(info)
        for name, info in servers.items()
        if isinstance(info, dict)
    }
    return data


def write_config(data: Dict[str, Any]) -> None:
    """Atomically write ``mcp_config.json`` with owner-only permissions."""
    path = Path(MCP_CONFIG_FILE).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".mcp_config.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        try:
            os.chmod(temp_path, 0o600)
        except OSError:  # pragma: no cover - non-POSIX filesystems
            pass
        os.replace(temp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise


def validate_server(
    name: str,
    command: str,
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    cwd: str = "",
) -> Dict[str, Any]:
    """Validate and normalize a stdio server definition."""
    name = str(name or "").strip()
    command = str(command or "").strip()
    if not name or len(name) > 64:
        raise ValueError("MCP Server 名称不能为空，且不能超过 64 个字符")
    if any(not (char.isalnum() or char in {"_", "-", "."}) for char in name):
        raise ValueError("MCP Server 名称只能包含字母、数字、下划线、短横线和点")
    if not command or len(command) > 2048:
        raise ValueError("启动命令不能为空，且不能超过 2048 个字符")
    clean_args = [str(item) for item in (args or [])]
    if len(clean_args) > 100:
        raise ValueError("MCP Server 参数不能超过 100 个")
    clean_env = {
        str(key): str(value)
        for key, value in (env or {}).items()
        if str(key).strip()
    }
    if len(clean_env) > 50:
        raise ValueError("MCP Server 环境变量不能超过 50 个")
    clean_cwd = str(cwd or "").strip()
    if len(clean_cwd) > 1024:
        raise ValueError("工作目录路径过长")
    return {
        "command": command,
        "args": clean_args,
        "env": clean_env,
        "cwd": clean_cwd,
    }


def list_configs(manager: Any) -> List[Dict[str, Any]]:
    """Return every configured server (including disabled ones) + status."""
    raw = read_config()
    status_map = {item["name"]: item for item in manager.list_server_status()}
    result: List[Dict[str, Any]] = []
    for name, info in raw["mcpServers"].items():
        state = status_map.get(name)
        status = str(state.get("status", "")) if state else "disabled"
        enabled = bool(info.get("enabled", True))
        if not enabled:
            status = "disabled"
        error = status[len("error:") :].strip() if status.startswith("error:") else ""
        result.append(
            {
                "name": name,
                "command": str(info.get("command") or ""),
                "args": [str(item) for item in (info.get("args") or [])],
                "env": {str(k): str(v) for k, v in (info.get("env") or {}).items()},
                "cwd": str(info.get("cwd") or ""),
                "description": str(info.get("description") or ""),
                "enabled": enabled,
                "transport": "stdio (official MCP SDK)",
                "status": status,
                "connected": bool(state and state.get("connected")),
                "tools_count": int(state.get("tools_count", 0)) if state else 0,
                "tools": list(state.get("tools") or []) if state else [],
                "error": error,
            }
        )
    result.sort(key=lambda item: (not item["enabled"], item["name"].lower()))
    return result


def upsert(
    manager: Any,
    name: str,
    *,
    command: str,
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    cwd: str = "",
    description: str = "",
    enabled: bool = True,
) -> Dict[str, Any]:
    """Create or replace a stdio MCP server and hot-reload clients."""
    clean = validate_server(name, command, args=args, env=env, cwd=cwd)
    name = str(name).strip()
    data = read_config()
    data["mcpServers"][name] = {
        **clean,
        "description": str(description or "").strip()[:500],
        "enabled": bool(enabled),
    }
    write_config(data)
    manager.load_config()
    return next(item for item in list_configs(manager) if item["name"] == name)


def delete(manager: Any, name: str) -> bool:
    """Remove a server from disk, stop its client, and reload the rest."""
    name = str(name or "").strip()
    data = read_config()
    if name not in data["mcpServers"]:
        return False
    data["mcpServers"].pop(name, None)
    write_config(data)
    client = manager.clients.pop(name, None)
    if client is not None:
        with contextlib.suppress(Exception):
            client.stop()
    manager.load_config()
    return True


def test_server(
    manager: Any,
    *,
    command: str,
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    cwd: str = "",
) -> Dict[str, Any]:
    """Start a temporary stdio server and report its tools."""
    clean = validate_server("__probe__", command, args=args, env=env, cwd=cwd)
    timeout = get_settings().mcp_request_timeout_seconds
    client = MCPClient(
        manager,
        "__probe__",
        clean["command"],
        clean["args"],
        timeout=timeout,
        env=clean["env"] or None,
        cwd=clean["cwd"] or None,
    )
    try:
        tools = client.list_tools()
        names = [
            str(
                item.get("name")
                if isinstance(item, dict)
                else getattr(item, "name", item)
            )
            for item in tools
        ]
        return {
            "ok": True,
            "tools_count": len(tools),
            "tools": names,
            "message": f"连接成功，发现 {len(tools)} 个工具",
        }
    except Exception as exc:
        return {
            "ok": False,
            "tools_count": 0,
            "tools": [],
            "message": f"连接失败: {exc}",
        }
    finally:
        with contextlib.suppress(Exception):
            client.stop()


def reload(manager: Any) -> List[Dict[str, Any]]:
    """Reload ``mcp_config.json`` and return all server configurations."""
    manager.load_config()
    return list_configs(manager)
