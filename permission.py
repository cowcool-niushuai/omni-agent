import re
import uuid
from typing import Any, Dict, Tuple

import database


class PermissionRisk:
    ALLOW = "ALLOW"             # Automatic execution
    SAFE_EXEC = "SAFE_EXEC"     # Safe with audit log
    ASK_USER = "ASK_USER"       # Requires human-in-the-loop confirmation
    DENY = "DENY"               # Completely blocked

DANGEROUS_PATTERNS = [
    r"\brm\b",
    r"\brmdir\b",
    r"\bkill\b",
    r"\bpkill\b",
    r"\bsudo\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bmkfs\b",
    r"\bdd\b",
    r">\s*/dev/",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",  # Fork bomb
    r"\|\s*bash",
    r"\|\s*sh"
]

DENIED_PATHS = [
    "/etc",
    "/root",
    "/sys",
    "/proc",
    "/boot"
]

def check_permission(tool_name: str, arguments: Dict[str, Any], session_id: str) -> Tuple[str, str, str]:
    """
    Returns: (RiskLevel, Reason, RequestId if ASK_USER else "")
    """
    # 1. Read-only tools are always allowed
    if tool_name in ["file_read", "file_list", "rag_search", "read_skill_doc", "mcp_db_schema", "mcp_db_stats"]:
        return PermissionRisk.ALLOW, "只读安全操作，自动放行", ""

    # 2. File write checks
    if tool_name in ["file_write", "file_edit"]:
        filepath = arguments.get("filepath", "")
        for dp in DENIED_PATHS:
            if filepath.startswith(dp):
                return PermissionRisk.DENY, f"严重越权：禁止修改系统核心目录 {dp}", ""
        # Overwrite check could be safe in workspace
        return PermissionRisk.SAFE_EXEC, "工作区文件操作，允许记录审计", ""

    # 3. Shell execution checks
    if tool_name == "bash_run":
        cmd = arguments.get("command", "").strip()
        for dp in DENIED_PATHS:
            if dp in cmd:
                return PermissionRisk.DENY, f"系统保护拦截：命令中包含敏感受限路径 {dp}", ""

        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                req_id = str(uuid.uuid4())[:8]
                reason = f"高危命令检测：命令包含潜在危险模式 '{pattern}'，触发企业级 Human-in-the-loop 安全拦截。"
                # Save permission request into database
                conn = database.get_db()
                cursor = conn.cursor()
                cursor.execute("""
                INSERT INTO permission_requests (id, session_id, tool_name, arguments, risk_level, reason, status)
                VALUES (?, ?, ?, ?, ?, ?, 'PENDING')
                """, (req_id, session_id, tool_name, str(arguments), PermissionRisk.ASK_USER, reason))
                conn.commit()
                conn.close()
                return PermissionRisk.ASK_USER, reason, req_id

        return PermissionRisk.SAFE_EXEC, "常规 Shell 指令，已通过沙箱预检", ""

    # 4. MCP SQL Query check
    if tool_name == "mcp_sql_query":
        query = arguments.get("query", "").strip().upper()
        if not query.startswith("SELECT"):
            return PermissionRisk.DENY, "SQL安全拦截：智能体仅允许执行只读 SELECT 查询，严禁 UPDATE/DELETE/DROP", ""
        return PermissionRisk.ALLOW, "只读 SQL 查询已放行", ""

    return PermissionRisk.ALLOW, "标准工具调用放行", ""

def approve_request(request_id: str) -> bool:
    conn = database.get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE permission_requests SET status = 'APPROVED' WHERE id = ?", (request_id,))
    conn.commit()
    conn.close()
    return True

def reject_request(request_id: str) -> bool:
    conn = database.get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE permission_requests SET status = 'REJECTED' WHERE id = ?", (request_id,))
    conn.commit()
    conn.close()
    return True

def get_request_status(request_id: str) -> str:
    conn = database.get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM permission_requests WHERE id = ?", (request_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row["status"]
    return "NOT_FOUND"
