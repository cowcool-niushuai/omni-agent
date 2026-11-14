"""Minimal MCP stdio client with timeouts, locking and reconnects.

The client speaks JSON-RPC 2.0 over a subprocess' stdin/stdout. Compared with
the original implementation it now:

* enforces a request timeout using ``select`` instead of blocking forever;
* serialises concurrent calls with a per-server lock;
* drains stderr so a chatty server cannot deadlock on a full pipe;
* restarts the server after a broken pipe or timeout;
* caps tool output before it enters the model context.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

from omni_agent.config import get_settings
from omni_agent.security.limits import truncate_text

MCP_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "mcp_config.json")

DEFAULT_CONFIG = {
    "mcpServers": {
        "sqlite_database": {
            "command": sys.executable,
            "args": [os.path.join(os.path.dirname(__file__), "mcp_servers", "sqlite_mcp.py")],
            "description": "企业 SQLite 核心数据持久化 MCP 服务，支持查询数据库表结构、统计数据和只读检索",
            "enabled": True
        }
    }
}


class MCPConnectionError(RuntimeError):
    """Transport failure that may be recovered by restarting the server."""


class MCPClient:
    def __init__(self, name: str, command: str, args: List[str], timeout: float = 15.0):
        self.name = name
        self.command = command
        self.args = args
        self.timeout = timeout
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self._msg_id = 0
        self._lock = threading.RLock()
        self._buffer = b""
        self._stderr_lines: deque[str] = deque(maxlen=50)
        self._stderr_thread: Optional[threading.Thread] = None
        self._tools_cache: List[Dict[str, Any]] = []

    # -- lifecycle ---------------------------------------------------------
    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self._alive():
                return
            self._spawn_locked()
            response = self._request_locked(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "omni-agent-client", "version": "1.0.0"},
                },
            )
            if "error" in response:
                self.stop()
                raise MCPConnectionError(
                    f"MCP Server {self.name} initialize failed: {response['error']}"
                )

    def _spawn_locked(self) -> None:
        self._buffer = b""
        try:
            self.proc = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise MCPConnectionError(
                f"MCP Server {self.name} 启动失败: {exc}"
            ) from exc
        self._stderr_lines.clear()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name=f"mcp-stderr-{self.name}",
            daemon=True,
        )
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                chunk = proc.stderr.readline()
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace").strip()
                if text:
                    self._stderr_lines.append(text)
        except (OSError, ValueError):
            return

    def _stderr_tail(self) -> str:
        return " | ".join(list(self._stderr_lines)[-5:])

    def stop(self) -> None:
        with self._lock:
            proc = self.proc
            self.proc = None
            if proc is None:
                return
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass

    # -- JSON-RPC ----------------------------------------------------------
    def _write_locked(self, payload: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise MCPConnectionError(f"MCP Server {self.name} 未启动")
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPConnectionError(f"MCP Server {self.name} 连接已断开: {exc}") from exc

    def _read_message_locked(self) -> dict[str, Any]:
        if self.proc is None or self.proc.stdout is None:
            raise MCPConnectionError(f"MCP Server {self.name} 未启动")
        deadline = time.monotonic() + self.timeout
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPConnectionError(
                    f"MCP Server {self.name} 响应超时 ({self.timeout:g}s)"
                )
            try:
                ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
            except (OSError, ValueError) as exc:
                raise MCPConnectionError(f"MCP Server {self.name} 读取失败: {exc}") from exc
            if not ready:
                raise MCPConnectionError(
                    f"MCP Server {self.name} 响应超时 ({self.timeout:g}s)"
                )
            try:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
            except OSError as exc:
                raise MCPConnectionError(f"MCP Server {self.name} 读取失败: {exc}") from exc
            if not chunk:
                tail = self._stderr_tail()
                raise MCPConnectionError(
                    f"MCP Server {self.name} 连接已关闭"
                    + (f"（stderr: {tail}）" if tail else "")
                )
            self._buffer += chunk

        line, _, rest = self._buffer.partition(b"\n")
        self._buffer = rest
        try:
            message = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise MCPConnectionError(
                f"MCP Server {self.name} 返回非法 JSON: {exc}"
            ) from exc
        if not isinstance(message, dict):
            raise MCPConnectionError(f"MCP Server {self.name} 返回格式异常")
        return message

    def _request_locked(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._msg_id += 1
        request_id = self._msg_id
        self._write_locked(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        # Servers may emit notifications before the response; skip them.
        for _ in range(50):
            message = self._read_message_locked()
            if message.get("id") == request_id:
                return message
        raise MCPConnectionError(f"MCP Server {self.name} 未返回匹配的响应")

    def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if not self._alive():
                self.start()
            try:
                return self._request_locked(method, params)
            except MCPConnectionError:
                self.stop()
                raise

    # -- tools -------------------------------------------------------------
    def list_tools(self, *, refresh: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            if self._tools_cache and not refresh:
                return list(self._tools_cache)
            response = self._send_request("tools/list", {})
            result = response.get("result")
            tools: List[Dict[str, Any]] = []
            if isinstance(result, dict) and isinstance(result.get("tools"), list):
                tools = [item for item in result["tools"] if isinstance(item, dict)]
            self._tools_cache = tools
            return list(tools)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        settings = get_settings()
        last_error = ""
        for attempt in range(2):
            try:
                response = self._send_request(
                    "tools/call", {"name": name, "arguments": arguments}
                )
                if "result" in response:
                    result = response["result"]
                    if isinstance(result, dict) and result.get("isError"):
                        content = result.get("content", [])
                        text = "\n".join(
                            str(item.get("text", ""))
                            for item in content
                            if isinstance(item, dict) and item.get("type") == "text"
                        )
                        return truncate_text(
                            f"MCP Error: {text or 'Unknown error'}",
                            settings.tool_max_output_chars,
                        )
                    content = (
                        result.get("content", [])
                        if isinstance(result, dict)
                        else []
                    )
                    text_blocks = [
                        str(item.get("text", ""))
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    ]
                    return truncate_text(
                        "\n".join(text_blocks) or "MCP 无文本内容返回。",
                        settings.tool_max_output_chars,
                    )
                if "error" in response:
                    error = response["error"]
                    message = error.get("message", "Unknown error") if isinstance(error, dict) else str(error)
                    return f"MCP Error: {message}"
                return "MCP 无内容返回。"
            except MCPConnectionError as exc:
                last_error = str(exc)
                self.stop()
                if attempt == 1:
                    break
                time.sleep(0.1)
        return f"MCP调用异常: {last_error}"

    def clear_cache(self) -> None:
        with self._lock:
            self._tools_cache = []


class MCPManager:
    def __init__(self) -> None:
        self.clients: Dict[str, MCPClient] = {}
        self.load_config()

    def load_config(self) -> None:
        if not os.path.exists(MCP_CONFIG_FILE):
            with open(MCP_CONFIG_FILE, "w", encoding="utf-8") as handle:
                json.dump(DEFAULT_CONFIG, handle, indent=2, ensure_ascii=False)
            cfg = DEFAULT_CONFIG
        else:
            try:
                with open(MCP_CONFIG_FILE, "r", encoding="utf-8") as handle:
                    cfg = json.load(handle)
            except (OSError, json.JSONDecodeError):
                cfg = DEFAULT_CONFIG

        timeout = get_settings().mcp_request_timeout_seconds
        servers = cfg.get("mcpServers", {})
        for server_name, server_info in servers.items():
            if not server_info.get("enabled", True):
                continue
            command = server_info.get("command", sys.executable)
            args = server_info.get("args", [])
            existing = self.clients.get(server_name)
            if (
                existing
                and existing.command == command
                and existing.args == args
                and existing.timeout == timeout
            ):
                continue
            if existing:
                existing.stop()
            self.clients[server_name] = MCPClient(
                server_name, command, args, timeout=timeout
            )

    def get_all_mcp_tools(self) -> List[Dict[str, Any]]:
        """Discover all tools and convert them to OpenAI function format."""
        tools_openai_format: List[Dict[str, Any]] = []
        for server_name, client in self.clients.items():
            try:
                for tool in client.list_tools():
                    tools_openai_format.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool["name"],
                                "description": f"[{server_name} MCP服务] {tool.get('description', '')}",
                                "parameters": tool.get(
                                    "inputSchema",
                                    {"type": "object", "properties": {}, "required": []},
                                ),
                            },
                            "_mcp_server": server_name,
                        }
                    )
            except Exception as exc:
                print(f"Error fetching tools from MCP Server {server_name}: {exc}")
        return tools_openai_format

    def call_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        for _server_name, client in self.clients.items():
            try:
                if any(tool.get("name") == tool_name for tool in client.list_tools()):
                    return client.call_tool(tool_name, arguments)
            except Exception as exc:
                return f"MCP调用异常: {exc}"
        return f"未找到提供工具 {tool_name} 的 MCP 服务"

    def list_server_status(self) -> List[Dict[str, Any]]:
        status: List[Dict[str, Any]] = []
        for name, client in self.clients.items():
            try:
                tools = client.list_tools()
                status.append(
                    {
                        "name": name,
                        "command": client.command,
                        "status": "connected",
                        "tools_count": len(tools),
                        "tools": [tool["name"] for tool in tools],
                    }
                )
            except Exception as exc:
                status.append(
                    {
                        "name": name,
                        "command": client.command,
                        "status": f"error: {exc}",
                        "tools_count": 0,
                        "tools": [],
                    }
                )
        return status

    def stop_all(self) -> None:
        for client in self.clients.values():
            client.stop()


mcp_manager = MCPManager()

if __name__ == "__main__":
    manager = MCPManager()
    print("MCP Servers status:", manager.list_server_status())
    tool_defs = manager.get_all_mcp_tools()
    print("Loaded MCP Tools:", [item["function"]["name"] for item in tool_defs])
    print("mcp_db_stats result:", manager.call_mcp_tool("mcp_db_stats", {}))
    manager.stop_all()
