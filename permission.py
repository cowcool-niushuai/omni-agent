"""Permission classification and human-in-the-loop request storage.

The classifier is pure. Requests are persisted only when a tool actually
requires approval, and request ids are deterministic so a LangGraph node can
safely re-run after ``interrupt()`` without creating duplicates.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

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
    r"\|\s*sh",
]

DENIED_PATHS = [
    "/etc",
    "/root",
    "/sys",
    "/proc",
    "/boot",
]


def permission_request_id(session_id: str, tool_call_id: str) -> str:
    """Stable id so replaying an interrupted node is idempotent."""
    digest = hashlib.sha256(f"{session_id}:{tool_call_id}".encode()).hexdigest()[:16]
    return f"perm_{digest}"


def permission_batch_id(session_id: str, tool_call_ids: list[str]) -> str:
    raw = f"{session_id}:" + ",".join(sorted(tool_call_ids))
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"batch_{digest}"


def classify_permission(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str]:
    """Classify a tool call without touching the database."""
    # 1. Read-only tools are always allowed
    if tool_name in [
        "file_read",
        "file_list",
        "rag_search",
        "read_skill_doc",
        "mcp_db_schema",
        "mcp_db_stats",
    ]:
        return PermissionRisk.ALLOW, "只读安全操作，自动放行"

    # 2. File write checks
    if tool_name in ["file_write", "file_edit"]:
        filepath = str(arguments.get("filepath", ""))
        for denied_path in DENIED_PATHS:
            if filepath.startswith(denied_path):
                return (
                    PermissionRisk.DENY,
                    f"严重越权：禁止修改系统核心目录 {denied_path}",
                )
        return PermissionRisk.SAFE_EXEC, "工作区文件操作，允许记录审计"

    # 3. Shell execution checks
    if tool_name == "bash_run":
        cmd = str(arguments.get("command", "")).strip()
        for denied_path in DENIED_PATHS:
            if denied_path in cmd:
                return (
                    PermissionRisk.DENY,
                    f"系统保护拦截：命令中包含敏感受限路径 {denied_path}",
                )

        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                reason = (
                    f"高危命令检测：命令包含潜在危险模式 '{pattern}'，"
                    "触发 Human-in-the-loop 安全拦截。"
                )
                return PermissionRisk.ASK_USER, reason

        return PermissionRisk.SAFE_EXEC, "常规 Shell 指令，已通过策略预检"

    # 4. MCP SQL Query check
    if tool_name == "mcp_sql_query":
        query = str(arguments.get("query", "")).strip().upper()
        if not query.startswith("SELECT"):
            return (
                PermissionRisk.DENY,
                "SQL安全拦截：智能体仅允许执行只读 SELECT 查询，"
                "严禁 UPDATE/DELETE/DROP",
            )
        return PermissionRisk.ALLOW, "只读 SQL 查询已放行"

    # Sub-agents inherit the same tool policy, but their own dangerous calls
    # are denied rather than pausing the parent graph.
    if tool_name == "spawn_subagent":
        return PermissionRisk.ALLOW, "子智能体任务将按工具策略执行"

    return PermissionRisk.ALLOW, "标准工具调用放行"


def create_permission_request(
    request_id: str,
    session_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    risk_level: str,
    reason: str,
) -> None:
    conn = database.get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO permission_requests
            (id, session_id, tool_name, arguments, risk_level, reason, status)
        VALUES (?, ?, ?, ?, ?, ?, 'PENDING')
        """,
        (
            request_id,
            session_id,
            tool_name,
            json.dumps(arguments, ensure_ascii=False),
            risk_level,
            reason,
        ),
    )
    conn.commit()
    conn.close()


def check_permission(
    tool_name: str,
    arguments: dict[str, Any],
    session_id: str,
    *,
    tool_call_id: str | None = None,
    persist: bool = True,
) -> tuple[str, str, str]:
    """Return ``(risk, reason, request_id)`` for one tool call."""
    risk, reason = classify_permission(tool_name, arguments)
    if risk != PermissionRisk.ASK_USER:
        return risk, reason, ""

    request_id = (
        permission_request_id(session_id, tool_call_id)
        if tool_call_id
        else str(uuid.uuid4())[:8]
    )
    if persist:
        create_permission_request(
            request_id, session_id, tool_name, arguments, risk, reason
        )
    return risk, reason, request_id


def _set_status(request_ids: list[str], status: str) -> None:
    if not request_ids:
        return
    placeholders = ",".join("?" for _ in request_ids)
    conn = database.get_db()
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE permission_requests SET status = ? WHERE id IN ({placeholders})",
        (status, *request_ids),
    )
    conn.commit()
    conn.close()


def approve_requests(request_ids: list[str]) -> None:
    _set_status(request_ids, "APPROVED")


def reject_requests(request_ids: list[str]) -> None:
    _set_status(request_ids, "REJECTED")


def approve_request(request_id: str) -> bool:
    approve_requests([request_id])
    return True


def reject_request(request_id: str) -> bool:
    reject_requests([request_id])
    return True


def get_request_status(request_id: str) -> str:
    conn = database.get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status FROM permission_requests WHERE id = ?", (request_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return str(row["status"])
    return "NOT_FOUND"


def pending_request_ids(session_id: str) -> list[str]:
    """All pending request ids for a session (used for batch approvals)."""
    conn = database.get_db()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id FROM permission_requests
        WHERE session_id = ? AND status = 'PENDING'
        ORDER BY created_at ASC
        """,
        (session_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [str(row["id"]) for row in rows]
