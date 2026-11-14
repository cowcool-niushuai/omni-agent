#!/usr/bin/env python3
"""
Standard MCP (Model Context Protocol) Server implementation for SQLite Database.
Communicates via JSON-RPC 2.0 over standard input / standard output (stdio).
"""
import json
import os
import sqlite3
import sys

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "omni_agent.db")

TOOLS = [
    {
        "name": "mcp_db_schema",
        "description": "通过 MCP 获取系统 SQLite 数据库中所有数据表的结构与字段定义。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "mcp_sql_query",
        "description": "通过 MCP 在 SQLite 数据库上执行只读 SQL 查询 (SELECT)，返回格式化行数据。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要执行的只读 SQL SELECT 语句。"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "mcp_db_stats",
        "description": "通过 MCP 统计当前智能体平台各表的数据行数与存储概况。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
]

def handle_list_tools():
    return {"tools": TOOLS}

def handle_call_tool(name: str, arguments: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        if name == "mcp_db_schema":
            cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            tables = [dict(r) for r in cursor.fetchall()]
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(tables, ensure_ascii=False, indent=2)
                    }
                ]
            }
            
        elif name == "mcp_sql_query":
            q = arguments.get("query", "").strip()
            if not q.upper().startswith("SELECT"):
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": "安全策略拦截：mcp_sql_query 仅允许执行只读 SELECT 查询！"}]
                }
            cursor.execute(q)
            rows = [dict(r) for r in cursor.fetchall()[:100]] # limit 100
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(rows, ensure_ascii=False, indent=2)
                    }
                ]
            }
            
        elif name == "mcp_db_stats":
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            tables = [r["name"] for r in cursor.fetchall()]
            stats = {}
            for t in tables:
                cursor.execute(f"SELECT count(*) as c FROM {t}")
                stats[t] = cursor.fetchone()["c"]
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(stats, ensure_ascii=False, indent=2)
                    }
                ]
            }
        else:
            return {"isError": True, "content": [{"type": "text", "text": f"未知工具: {name}"}]}
    except Exception as e:
        return {"isError": True, "content": [{"type": "text", "text": f"MCP 数据库查询异常: {str(e)}"}]}
    finally:
        conn.close()

def main():
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params", {})
            
            if method == "initialize":
                res = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "omni-sqlite-mcp", "version": "1.0.0"}
                    }
                }
            elif method == "tools/list":
                res = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": handle_list_tools()
                }
            elif method == "tools/call":
                tool_name = params.get("name")
                tool_args = params.get("arguments", {})
                res = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": handle_call_tool(tool_name, tool_args)
                }
            else:
                res = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"}
                }
            sys.stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception as e:
            err_res = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(e)}
            }
            sys.stdout.write(json.dumps(err_res, ensure_ascii=False) + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()
