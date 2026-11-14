"""MCP client backed by the official Model Context Protocol Python SDK.

The application's tool layer is synchronous (the LangGraph runtime executes
native tools in worker threads), while the official SDK is asyncio-only.  This
module bridges the two worlds:

* one dedicated event-loop thread owns every MCP connection;
* each server gets a long-lived worker task that enters ``stdio_client`` and
  ``ClientSession`` in the *same* task, which is required by anyio's cancel
  scopes;
* synchronous callers submit operations through a queue and wait with a
  timeout, so a broken server can never block the agent forever;
* failures tear down the worker, reconnect with bounded backoff, and retry the
  call once.

Compared with the previous hand-written JSON-RPC client this uses the SDK's
protocol negotiation, request/response dispatch, cancellation and content
models instead of reimplementing them.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Dict, List, Optional, TextIO, cast

import mcp.types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from omni_agent.config import get_settings
from omni_agent.security.limits import truncate_text

MCP_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "mcp_config.json")

DEFAULT_CONFIG = {
    "mcpServers": {
        "sqlite_database": {
            "command": sys.executable,
            "args": [
                os.path.join(
                    os.path.dirname(__file__), "mcp_servers", "sqlite_mcp.py"
                )
            ],
            "description": "企业 SQLite 核心数据持久化 MCP 服务，支持查询数据库表结构、统计数据和只读检索",
            "enabled": True,
        }
    }
}


class MCPConnectionError(RuntimeError):
    """Transport or protocol failure that may be recovered by reconnecting."""


class _StderrBuffer:
    """TextIO sink so the SDK subprocess can redirect stderr to a temp file."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self) -> None:
        self._file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            written = self._file.write(str(text))
            if self._file.tell() > 1_000_000:
                # Keep the file bounded; diagnostics only need the tail.
                self._file.seek(0)
                self._file.truncate()
            return written

    def flush(self) -> None:
        with self._lock:
            self._file.flush()

    def fileno(self) -> int:
        return self._file.fileno()

    def tail(self) -> str:
        with self._lock:
            try:
                self._file.flush()
                end = self._file.tell()
                self._file.seek(max(0, end - 8192))
                text = self._file.read()
            except (OSError, ValueError):
                return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return " | ".join(lines[-5:])


class MCPClient:
    """One MCP stdio server, driven by a worker task on the manager loop."""

    def __init__(
        self,
        manager: "MCPManager",
        name: str,
        command: str,
        args: List[str],
        timeout: float = 15.0,
        *,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
    ) -> None:
        self._manager = manager
        self.name = name
        self.command = command
        self.args = list(args)
        self.timeout = timeout
        self.env = dict(env or {})
        self.cwd = cwd
        self._queue: Optional[asyncio.Queue[tuple[str, dict[str, Any], asyncio.Future[Any]]]] = None
        self._worker: Optional[asyncio.Task[None]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stderr = _StderrBuffer()
        self._tools_cache: List[Dict[str, Any]] = []
        self._last_error = ""
        self._started_at: Optional[float] = None
        self._closed = False

    # -- sync facade -------------------------------------------------------
    def start(self) -> None:
        """Eagerly connect; raises ``MCPConnectionError`` on failure."""
        try:
            self._manager._submit(self._connect_probe(), timeout=self.timeout + 3)
        except MCPConnectionError:
            raise
        except Exception as exc:
            raise MCPConnectionError(
                f"MCP Server {self.name} 启动失败: {exc}"
            ) from exc

    async def _connect_probe(self) -> None:
        await self._request("ping", {})

    def list_tools(self, *, refresh: bool = False) -> List[Dict[str, Any]]:
        if self._tools_cache and not refresh:
            return list(self._tools_cache)
        try:
            tools = self._manager._submit(
                self._request("list_tools", {}), timeout=self.timeout + 3
            )
        except MCPConnectionError:
            raise
        except Exception as exc:
            raise MCPConnectionError(
                f"MCP Server {self.name} 工具发现失败: {exc}"
            ) from exc
        if not isinstance(tools, list):
            raise MCPConnectionError(f"MCP Server {self.name} 工具列表格式异常")
        self._tools_cache = [item for item in tools if isinstance(item, dict)]
        return list(self._tools_cache)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        settings = get_settings()
        last_error = ""
        for attempt in range(2):
            try:
                result = self._manager._submit(
                    self._request("call_tool", {"name": name, "arguments": arguments}),
                    timeout=self.timeout + 3,
                )
                return truncate_text(str(result), settings.tool_max_output_chars)
            except MCPConnectionError as exc:
                last_error = str(exc)
                if attempt == 1:
                    break
                time.sleep(0.1)
            except Exception as exc:
                last_error = str(exc)
                break
        return f"MCP调用异常: {last_error}"

    def clear_cache(self) -> None:
        self._tools_cache = []

    def stop(self) -> None:
        try:
            self._manager._submit(self._shutdown(), timeout=5)
        except Exception:
            # Shutdown is best effort: a crashed server must not block exit.
            self._closed = True
            worker = self._worker
            if worker is not None and self._loop is not None:
                with contextlib.suppress(Exception):
                    self._loop.call_soon_threadsafe(worker.cancel)

    def _tail(self) -> str:
        return self._stderr.tail()

    def status(self) -> dict[str, Any]:
        if self._last_error:
            return {"status": f"error: {self._last_error}", "connected": False}
        if self._worker is not None and not self._worker.done():
            return {"status": "connected", "connected": True}
        return {"status": "disconnected", "connected": False}

    # -- async worker ------------------------------------------------------
    def _ensure_worker(self) -> None:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        if self._queue is None:
            self._queue = asyncio.Queue()
        if self._worker is None or self._worker.done():
            self._closed = False
            self._worker = asyncio.create_task(
                self._worker_loop(), name=f"mcp-worker-{self.name}"
            )
            self._worker.add_done_callback(self._on_worker_done)

    async def _request(self, op: str, payload: dict[str, Any]) -> Any:
        self._ensure_worker()
        assert self._queue is not None
        assert self._loop is not None
        future: asyncio.Future[Any] = self._loop.create_future()
        await self._queue.put((op, payload, future))
        return await future

    def _on_worker_done(self, task: asyncio.Task[None]) -> None:
        if self._closed or task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._last_error = str(exc)
        self._fail_pending(
            MCPConnectionError(
                f"MCP Server {self.name} 连接已断开"
                + (f"（stderr: {self._tail()}）" if self._tail() else "")
            )
        )

    def _fail_pending(self, error: Exception) -> None:
        if self._queue is None:
            return
        while True:
            try:
                _op, _payload, future = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not future.done():
                future.set_exception(error)

    async def _worker_loop(self) -> None:
        failures = 0
        delay = 0.2
        while not self._closed:
            try:
                params = StdioServerParameters(
                    command=self.command,
                    args=self.args,
                    env=self.env or None,
                    cwd=self.cwd or None,
                )
                async with stdio_client(
                    params, errlog=cast(TextIO, self._stderr)
                ) as (read, write):
                    async with ClientSession(
                        read, write, read_timeout_seconds=self.timeout
                    ) as session:
                        await asyncio.wait_for(session.initialize(), self.timeout)
                        self._started_at = time.time()
                        self._last_error = ""
                        failures = 0
                        delay = 0.2
                        while not self._closed:
                            assert self._queue is not None
                            op, payload, future = await self._queue.get()
                            if op == "stop":
                                if not future.done():
                                    future.set_result(None)
                                return
                            try:
                                result = await self._handle(session, op, payload)
                            except Exception as exc:
                                self._last_error = str(exc)
                                if not future.done():
                                    future.set_exception(
                                        MCPConnectionError(
                                            f"MCP Server {self.name}: {exc}"
                                        )
                                    )
                                # The transport may be wedged; unwind and reconnect.
                                raise
                            if not future.done():
                                future.set_result(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                failures += 1
                if failures >= 3:
                    self._fail_pending(
                        MCPConnectionError(
                            f"MCP Server {self.name} 连接失败: {exc}"
                            + (
                                f"（stderr: {self._tail()}）"
                                if self._tail()
                                else ""
                            )
                        )
                    )
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2.0)

    async def _handle(
        self, session: ClientSession, op: str, payload: dict[str, Any]
    ) -> Any:
        if op == "ping":
            return "pong"
        if op == "list_tools":
            return await self._list_tools(session)
        if op == "call_tool":
            return await self._call_tool(
                session,
                str(payload.get("name", "")),
                dict(payload.get("arguments") or {}),
            )
        raise MCPConnectionError(f"未知的 MCP 操作: {op}")

    async def _list_tools(self, session: ClientSession) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(20):
            params = (
                types.PaginatedRequestParams(cursor=cursor) if cursor else None
            )
            result = await session.list_tools(params=params)
            for tool in result.tools:
                dumped = tool.model_dump(by_alias=True, exclude_none=True)
                if isinstance(dumped, dict):
                    tools.append(dumped)
            cursor = result.next_cursor
            if not cursor:
                break
        return tools

    async def _call_tool(
        self, session: ClientSession, name: str, arguments: dict[str, Any]
    ) -> str:
        result = await session.call_tool(name, arguments=arguments)
        content = getattr(result, "content", None) or []
        texts: List[str] = []
        for block in content:
            block_type = getattr(block, "type", "")
            if isinstance(block, types.TextContent):
                texts.append(block.text)
            elif block_type == "image":
                texts.append("[MCP 返回了一张图片，已省略二进制内容]")
            elif block_type == "audio":
                texts.append("[MCP 返回了音频内容，已省略二进制内容]")
            elif block_type == "resource_link":
                texts.append(f"[资源链接] {getattr(block, 'uri', '')}")
            else:
                texts.append(str(block))
        if not texts:
            structured = getattr(result, "structured_content", None)
            if structured is not None:
                texts.append(json.dumps(structured, ensure_ascii=False))
        text = "\n".join(texts) or "MCP 无文本内容返回。"
        if getattr(result, "is_error", False):
            return f"MCP Error: {text}"
        return text

    async def _shutdown(self) -> None:
        self._closed = True
        worker = self._worker
        if worker is None:
            return
        try:
            if not worker.done() and self._queue is not None:
                future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
                await self._queue.put(("stop", {}, future))
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(asyncio.shield(future), 2.0)
            if not worker.done():
                await asyncio.wait_for(asyncio.shield(worker), 3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        finally:
            self._worker = None
            self._started_at = None
            self._fail_pending(MCPConnectionError(f"MCP Server {self.name} 已停止"))


class MCPManager:
    """Synchronous facade over an isolated asyncio event-loop thread."""

    def __init__(self) -> None:
        self.clients: Dict[str, MCPClient] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._loop_lock = threading.Lock()
        self.load_config()
        atexit.register(self.stop_all)

    # -- loop bridge -------------------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if (
                self._loop is not None
                and self._thread is not None
                and self._thread.is_alive()
            ):
                return self._loop
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_loop,
                args=(self._loop,),
                name="mcp-event-loop",
                daemon=True,
            )
            self._thread.start()
            return self._loop

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _submit(self, coro: Any, *, timeout: float) -> Any:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise MCPConnectionError(f"MCP 操作超时 ({timeout:g}s)") from exc

    # -- configuration -----------------------------------------------------
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
        wanted: Dict[str, MCPClient] = {}
        if not isinstance(servers, dict):
            servers = {}
        for server_name, raw_info in servers.items():
            if not isinstance(raw_info, dict):
                continue
            server_info: dict[str, Any] = raw_info
            if not server_info.get("enabled", True):
                continue
            command = str(server_info.get("command", sys.executable))
            raw_args = server_info.get("args", [])
            args = [str(item) for item in raw_args] if isinstance(raw_args, list) else []
            raw_env_value = server_info.get("env")
            raw_env = raw_env_value if isinstance(raw_env_value, dict) else {}
            env = {
                str(key): str(value)
                for key, value in raw_env.items()
                if value is not None
            }
            cwd = server_info.get("cwd")
            cwd_value = str(cwd) if cwd else None
            existing = self.clients.get(server_name)
            if (
                existing
                and existing.command == command
                and existing.args == args
                and existing.timeout == timeout
                and existing.env == env
                and existing.cwd == cwd_value
            ):
                wanted[server_name] = existing
                continue
            if existing:
                existing.stop()
            wanted[server_name] = MCPClient(
                self,
                server_name,
                command,
                args,
                timeout=timeout,
                env=env,
                cwd=cwd_value,
            )
        for name, client in self.clients.items():
            if name not in wanted:
                client.stop()
        self.clients = wanted

    # -- public sync API ---------------------------------------------------
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
                                "description": (
                                    f"[{server_name} MCP服务] "
                                    f"{tool.get('description', '')}"
                                ),
                                "parameters": tool.get(
                                    "inputSchema",
                                    {
                                        "type": "object",
                                        "properties": {},
                                        "required": [],
                                    },
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
                state = client.status()
                status.append(
                    {
                        "name": name,
                        "command": client.command,
                        "transport": "stdio (official MCP SDK)",
                        "status": state["status"],
                        "connected": state["connected"],
                        "tools_count": len(tools),
                        "tools": [tool["name"] for tool in tools],
                    }
                )
            except Exception as exc:
                status.append(
                    {
                        "name": name,
                        "command": client.command,
                        "transport": "stdio (official MCP SDK)",
                        "status": f"error: {exc}",
                        "connected": False,
                        "tools_count": 0,
                        "tools": [],
                    }
                )
        return status

    def stop_all(self) -> None:
        for client in list(self.clients.values()):
            client.stop()


mcp_manager = MCPManager()

if __name__ == "__main__":
    manager = MCPManager()
    print("MCP Servers status:", manager.list_server_status())
    tool_defs = manager.get_all_mcp_tools()
    print("Loaded MCP Tools:", [item["function"]["name"] for item in tool_defs])
    print("mcp_db_stats result:", manager.call_mcp_tool("mcp_db_stats", {}))
    manager.stop_all()
