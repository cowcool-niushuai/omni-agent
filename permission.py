"""Permission classification and human-in-the-loop request storage.

The classifier is pure: it never executes the tool and (except for approval
request persistence) never touches the database. Shell command and URL checks
delegate to :mod:`omni_agent.security`, so the policy engine is shared by the
runtime, sub-agents and direct tool calls.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import database
from omni_agent.config import get_settings
from omni_agent.security.commands import (
    ASK_USER as COMMAND_ASK_USER,
)
from omni_agent.security.commands import (
    DENY as COMMAND_DENY,
)
from omni_agent.security.commands import (
    assess_command,
    validate_sql_readonly,
)
from omni_agent.security.paths import PathSecurityError, safe_join
from omni_agent.security.ssrf import UnsafeURLError, validate_url_static
from omni_agent.tenancy import current_project_id


class PermissionRisk:
    ALLOW = "ALLOW"             # Automatic execution
    SAFE_EXEC = "SAFE_EXEC"     # Safe with audit log
    ASK_USER = "ASK_USER"       # Requires human-in-the-loop confirmation
    DENY = "DENY"               # Completely blocked


READ_ONLY_TOOLS = {
    "file_read",
    "file_list",
    "web_search",
    "rag_search",
    "memory_search",
    "read_skill_doc",
    "update_plan",
    "mcp_db_schema",
    "mcp_db_stats",
}
WRITE_TOOLS = {"file_write", "file_edit"}
MEMORY_WRITE_TOOLS = {"memory_save", "knowledge_add"}


def permission_request_id(session_id: str, tool_call_id: str) -> str:
    """Stable id so replaying an interrupted node is idempotent."""
    digest = hashlib.sha256(f"{session_id}:{tool_call_id}".encode()).hexdigest()[:16]
    return f"perm_{digest}"


def permission_batch_id(session_id: str, tool_call_ids: list[str]) -> str:
    raw = f"{session_id}:" + ",".join(sorted(tool_call_ids))
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"batch_{digest}"


def _workspace_root() -> Path:
    from omni_agent import tenancy

    return tenancy.current_workspace().resolve()


def _is_full_access() -> bool:
    from omni_agent import tenancy

    return tenancy.is_full_access()


def _check_workspace_path(filepath: str) -> tuple[bool, str]:
    if _is_full_access():
        return True, ""
    try:
        safe_join(_workspace_root(), filepath)
    except PathSecurityError as exc:
        return False, str(exc)
    return True, ""


def _check_file_size(arguments: dict[str, Any]) -> tuple[bool, int]:
    settings = get_settings()
    content = arguments.get("content")
    replacement = arguments.get("replacement")
    size = 0
    if isinstance(content, str):
        size = len(content.encode("utf-8"))
    if isinstance(replacement, str):
        size = max(size, len(replacement.encode("utf-8")))
    return size <= settings.max_file_write_bytes, size


def classify_permission(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str]:
    """Classify a tool call without executing it."""
    # 1. Read-only tools are always allowed
    if tool_name in READ_ONLY_TOOLS:
        return PermissionRisk.ALLOW, "只读安全操作，自动放行"

    # 2. File writes: containment + size checks
    if tool_name in WRITE_TOOLS:
        filepath = str(arguments.get("filepath", ""))
        if not filepath.strip():
            return PermissionRisk.DENY, "文件路径不能为空"
        ok, reason = _check_workspace_path(filepath)
        if not ok:
            return PermissionRisk.DENY, f"严重越权：文件路径不在工作区 ({reason})"
        size_ok, size = _check_file_size(arguments)
        if not size_ok:
            limit_mb = get_settings().max_file_write_bytes // (1024 * 1024)
            return PermissionRisk.DENY, f"写入内容过大（{size} bytes，上限 {limit_mb} MB）"
        if tool_name == "file_edit":
            target = str(arguments.get("target", ""))
            if not target:
                return PermissionRisk.DENY, "file_edit 缺少 target 参数"
        return PermissionRisk.SAFE_EXEC, "工作区文件操作，允许记录审计"

    # 2b. Agent-managed memory and knowledge are local SQLite writes.
    if tool_name in MEMORY_WRITE_TOOLS:
        settings = get_settings()
        if tool_name == "memory_save":
            key = str(arguments.get("key", "")).strip()
            content = str(arguments.get("content", "")).strip()
            if not key or not content:
                return PermissionRisk.DENY, "记忆的 key 和 content 不能为空"
            if len(content) > settings.memory_max_content_chars:
                return (
                    PermissionRisk.DENY,
                    f"记忆内容过长（上限 {settings.memory_max_content_chars} 字符）",
                )
        else:
            title = str(arguments.get("title", "")).strip()
            content = str(arguments.get("content", "")).strip()
            if not title or not content:
                return PermissionRisk.DENY, "知识文档的 title 和 content 不能为空"
            if len(title) > 200:
                return PermissionRisk.DENY, "知识文档标题过长（上限 200 字符）"
            if len(content) > settings.knowledge_max_content_chars:
                return (
                    PermissionRisk.DENY,
                    f"知识文档过长（上限 {settings.knowledge_max_content_chars} 字符）",
                )
        return PermissionRisk.SAFE_EXEC, "本地记忆/知识库写入，允许记录审计"

    # 3. Shell execution: shared command policy engine
    if tool_name == "bash_run":
        settings = get_settings()
        command = str(arguments.get("command", ""))
        assessment = assess_command(
            command,
            _workspace_root(),
            policy_mode=(
                "full_access"
                if _is_full_access()
                else settings.command_policy_mode
            ),
        )
        if assessment.risk == COMMAND_DENY:
            return PermissionRisk.DENY, assessment.reason
        if assessment.risk == COMMAND_ASK_USER:
            return PermissionRisk.ASK_USER, assessment.reason
        return assessment.risk, assessment.reason

    # 4. Arbitrary Python runs inside the configured sandbox.
    if tool_name == "python_eval":
        code = str(arguments.get("code", ""))
        if not code.strip():
            return PermissionRisk.DENY, "Python 代码为空"
        settings = get_settings()
        from omni_agent.security.sandbox import detect_backend

        if _is_full_access():
            return (
                PermissionRisk.SAFE_EXEC,
                "完全访问模式：Python 代码将在主机环境执行",
            )
        backend = detect_backend(settings.sandbox_backend)
        return (
            PermissionRisk.SAFE_EXEC,
            f"Python 代码将在 {backend} 沙箱中受限执行",
        )

    # 5. Network fetch: static SSRF pre-check (full DNS check at execution)
    if tool_name == "web_fetch":
        settings = get_settings()
        try:
            validate_url_static(
                str(arguments.get("url", "")),
                allowed_ports=settings.web_fetch_port_list,
                allow_private=settings.allow_private_network,
            )
        except UnsafeURLError as exc:
            return PermissionRisk.DENY, f"网页抓取被 SSRF 策略阻止：{exc}"
        return PermissionRisk.ALLOW, "公网网页只读抓取已放行"

    # 5b. Headless browser: navigation is read-only; interactions need approval.
    if tool_name == "browser":
        settings = get_settings()
        if not settings.browser_enabled:
            return PermissionRisk.DENY, "浏览器自动化已在配置中禁用"
        action = str(arguments.get("action", "")).strip().lower()
        supported = {
            "goto",
            "click",
            "fill",
            "press",
            "text",
            "screenshot",
            "wait",
            "back",
            "forward",
            "reload",
            "status",
            "close",
        }
        if action not in supported:
            return PermissionRisk.DENY, f"不支持的浏览器操作: {action or '(空)'}"
        if action == "goto":
            try:
                validate_url_static(
                    str(arguments.get("url", "")),
                    allowed_ports=settings.web_fetch_port_list,
                    allow_private=settings.allow_private_network,
                )
            except UnsafeURLError as exc:
                return PermissionRisk.DENY, f"浏览器导航被 SSRF 策略阻止：{exc}"
            return PermissionRisk.ALLOW, "公网网页只读导航已放行"
        if action in {"click", "fill", "press"}:
            if _is_full_access():
                return PermissionRisk.ALLOW, "完全访问模式：浏览器交互自动放行"
            return (
                PermissionRisk.ASK_USER,
                "浏览器交互可能提交表单或改变网站状态，需要人工确认",
            )
        if action == "screenshot":
            filename = str(arguments.get("filename", "")).strip()
            if filename:
                try:
                    path = safe_join(_workspace_root() / "browser", filename)
                except PathSecurityError as exc:
                    return PermissionRisk.DENY, f"截图路径越权: {exc}"
                if path.suffix.lower() != ".png":
                    return PermissionRisk.DENY, "截图文件名必须以 .png 结尾"
            return PermissionRisk.SAFE_EXEC, "浏览器截图写入工作区，允许记录审计"
        return PermissionRisk.ALLOW, "浏览器只读操作已放行"

    # 6. MCP SQL Query check
    if tool_name == "mcp_sql_query":
        ok, reason = validate_sql_readonly(str(arguments.get("query", "")))
        if not ok:
            return PermissionRisk.DENY, f"SQL安全拦截：{reason}"
        return PermissionRisk.ALLOW, reason

    # Sub-agents inherit the same tool policy, but their own dangerous calls
    # are denied rather than pausing the parent graph.
    if tool_name == "spawn_subagent":
        return PermissionRisk.ALLOW, "子智能体任务将按工具策略执行"

    # Configured MCP tools are trusted extensions. Unknown native tools are not.
    if tool_name.startswith("mcp_"):
        return PermissionRisk.ALLOW, "已配置 MCP 工具调用放行"

    if _is_full_access():
        return PermissionRisk.ALLOW, "完全访问模式：工具调用自动放行"
    return PermissionRisk.ASK_USER, f"未注册的工具 {tool_name} 需要人工确认"


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
            (id, session_id, tool_name, arguments, risk_level, reason, status, project_id)
        VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)
        """,
        (
            request_id,
            session_id,
            tool_name,
            json.dumps(arguments, ensure_ascii=False),
            risk_level,
            reason,
            current_project_id(),
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
    if _is_full_access() and risk == PermissionRisk.ASK_USER:
        risk = PermissionRisk.ALLOW
        reason = "完全访问模式：高风险确认已关闭"
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


def get_request(request_id: str) -> dict[str, Any] | None:
    conn = database.get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM permission_requests WHERE id = ?", (request_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_request_status(request_id: str) -> str:
    request = get_request(request_id)
    return str(request["status"]) if request else "NOT_FOUND"


def pending_request_ids(
    session_id: str, *, project_id: str | None = None
) -> list[str]:
    """All pending request ids for a session (used for batch approvals)."""
    conn = database.get_db()
    cursor = conn.cursor()
    if project_id:
        cursor.execute(
            """
            SELECT id FROM permission_requests
            WHERE session_id = ? AND project_id = ? AND status = 'PENDING'
            ORDER BY created_at ASC
            """,
            (session_id, project_id),
        )
    else:
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
