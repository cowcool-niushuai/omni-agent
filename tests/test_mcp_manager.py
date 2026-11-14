"""Tests for the official MCP SDK client bridge."""

from __future__ import annotations

import sys
import textwrap

from mcp_manager import MCPClient, MCPManager

TEST_SERVER = textwrap.dedent(
    """
    import json
    import sys

    TOOLS = [
        {
            "name": "echo",
            "description": "Echo text",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
        {
            "name": "fail",
            "description": "Always fails",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]

    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        if "id" not in request:
            # Notifications such as notifications/initialized.
            continue
        request_id = request["id"]
        method = request.get("method")
        params = request.get("params") or {}
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-mcp", "version": "1.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                result = {
                    "content": [{"type": "text", "text": arguments.get("text", "")}]
                }
            elif name == "fail":
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": "boom"}],
                }
            else:
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": "unknown tool"}],
                }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"unknown method: {method}"},
            }
            print(json.dumps(response), flush=True)
            continue
        print(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}),
            flush=True,
        )
    """
).strip()


def test_official_sdk_client_list_and_call(tmp_path):
    script = tmp_path / "test_mcp_server.py"
    script.write_text(TEST_SERVER, encoding="utf-8")

    manager = MCPManager()
    client = MCPClient(
        manager,
        "test",
        sys.executable,
        [str(script)],
        timeout=5,
    )
    try:
        tools = client.list_tools()
        assert [tool["name"] for tool in tools] == ["echo", "fail"]
        assert tools[0]["inputSchema"]["required"] == ["text"]

        assert client.call_tool("echo", {"text": "你好 MCP"}) == "你好 MCP"
        assert client.call_tool("fail", {}) == "MCP Error: boom"
        assert client.status()["connected"] is True

        # The cache avoids a second tools/list round trip.
        assert client.list_tools() is not tools

        manager.clients = {"test": client}
        definitions = manager.get_all_mcp_tools()
        assert [item["function"]["name"] for item in definitions] == [
            "echo",
            "fail",
        ]
        assert definitions[0]["_mcp_server"] == "test"
        assert manager.call_mcp_tool("echo", {"text": "routed"}) == "routed"
    finally:
        client.stop()
        manager.stop_all()
