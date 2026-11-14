import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

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

class MCPClient:
    def __init__(self, name: str, command: str, args: List[str]):
        self.name = name
        self.command = command
        self.args = args
        self.proc: Optional[subprocess.Popen] = None
        self._msg_id = 0

    def start(self):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            # Handshake
            self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "omni-agent-client", "version": "1.0.0"}
            })

    def _send_request(self, method: str, params: dict) -> dict:
        self.start()
        self._msg_id += 1
        req = {
            "jsonrpc": "2.0",
            "id": self._msg_id,
            "method": method,
            "params": params
        }
        req_line = json.dumps(req, ensure_ascii=False) + "\n"
        self.proc.stdin.write(req_line)
        self.proc.stdin.flush()
        
        resp_line = self.proc.stdout.readline()
        if not resp_line:
            raise RuntimeError(f"MCP Server {self.name} connection closed.")
        return json.loads(resp_line)

    def list_tools(self) -> List[Dict[str, Any]]:
        res = self._send_request("tools/list", {})
        if "result" in res and "tools" in res["result"]:
            return res["result"]["tools"]
        return []

    def call_tool(self, name: str, arguments: dict) -> str:
        res = self._send_request("tools/call", {"name": name, "arguments": arguments})
        if "result" in res:
            content = res["result"].get("content", [])
            text_blocks = [c.get("text", "") for c in content if c.get("type") == "text"]
            return "\n".join(text_blocks)
        elif "error" in res:
            return f"MCP Error: {res['error'].get('message', 'Unknown error')}"
        return "No content returned."

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=2)
            self.proc = None

class MCPManager:
    def __init__(self):
        self.clients: Dict[str, MCPClient] = {}
        self.load_config()

    def load_config(self):
        if not os.path.exists(MCP_CONFIG_FILE):
            with open(MCP_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
            cfg = DEFAULT_CONFIG
        else:
            with open(MCP_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)

        servers = cfg.get("mcpServers", {})
        for s_name, s_info in servers.items():
            if s_info.get("enabled", True):
                cmd = s_info.get("command", sys.executable)
                args = s_info.get("args", [])
                self.clients[s_name] = MCPClient(s_name, cmd, args)

    def get_all_mcp_tools(self) -> List[Dict[str, Any]]:
        """
        Discovers all tools across enabled MCP servers and converts them into OpenAI function calling format.
        """
        tools_openai_format = []
        for server_name, client in self.clients.items():
            try:
                mcp_tools = client.list_tools()
                for t in mcp_tools:
                    # Convert to OpenAI tool format
                    tools_openai_format.append({
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": f"[{server_name} MCP服务] {t['description']}",
                            "parameters": t.get("inputSchema", {
                                "type": "object",
                                "properties": {},
                                "required": []
                            })
                        },
                        "_mcp_server": server_name
                    })
            except Exception as e:
                print(f"Error fetching tools from MCP Server {server_name}: {e}")
        return tools_openai_format

    def call_mcp_tool(self, tool_name: str, arguments: dict) -> str:
        for _server_name, client in self.clients.items():
            try:
                tools = client.list_tools()
                for t in tools:
                    if t["name"] == tool_name:
                        return client.call_tool(tool_name, arguments)
            except Exception as e:
                return f"MCP调用异常: {str(e)}"
        return f"未找到提供工具 {tool_name} 的 MCP 服务"

    def list_server_status(self) -> List[Dict[str, Any]]:
        status = []
        for name, client in self.clients.items():
            try:
                tools = client.list_tools()
                status.append({
                    "name": name,
                    "command": client.command,
                    "status": "connected",
                    "tools_count": len(tools),
                    "tools": [t["name"] for t in tools]
                })
            except Exception as e:
                status.append({
                    "name": name,
                    "command": client.command,
                    "status": f"error: {str(e)}",
                    "tools_count": 0,
                    "tools": []
                })
        return status

mcp_manager = MCPManager()

if __name__ == "__main__":
    mgr = MCPManager()
    print("MCP Servers status:", mgr.list_server_status())
    tools = mgr.get_all_mcp_tools()
    print("Loaded MCP Tools:", [t["function"]["name"] for t in tools])
    # Test call
    res = mgr.call_mcp_tool("mcp_db_stats", {})
    print("mcp_db_stats result:", res)
