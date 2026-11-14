"""Tool execution audit trail.

Every native/MCP tool call is recorded with its risk classification, decision
(auto / approved / rejected / denied), outcome, duration, exit code and a
redacted argument/output preview. The audit write must never break tool
execution, so failures are swallowed and reported through structlog.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

import database
from omni_agent.tenancy import current_project_id, current_user_id

log = structlog.get_logger("omni.audit")

_SECRET_KEY = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.IGNORECASE)
_OUTPUT_SECRET = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9._-]{8,}|"
    r"api[_-]?key\s*[:=]\s*[^\s,;]+|token\s*[:=]\s*[^\s,;]+)"
)
_PREVIEW_CHARS = 500
_ARGUMENT_CHARS = 300


def _redact(value: Any, key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value[:20]]
    if isinstance(value, str) and len(value) > _ARGUMENT_CHARS:
        return value[: _ARGUMENT_CHARS] + "...[truncated]"
    return value


def record_tool_call(
    *,
    session_id: str,
    turn_id: str | None,
    tool_call_id: str | None,
    tool_name: str,
    arguments: dict[str, Any],
    risk_level: str,
    decision: str,
    status: str,
    sandbox: str | None = None,
    exit_code: int | None = None,
    duration_ms: int = 0,
    output_chars: int = 0,
    output_preview: str = "",
    error: str | None = None,
    error_code: str | None = None,
    project_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Persist one audit row. Never raises."""
    try:
        safe_arguments = json.dumps(_redact(arguments), ensure_ascii=False)
        preview = _OUTPUT_SECRET.sub("***REDACTED***", output_preview[:_PREVIEW_CHARS])
        conn = database.get_db()
        try:
            conn.execute(
                """
                INSERT INTO tool_audit (
                    session_id, turn_id, tool_call_id, tool_name, arguments,
                    risk_level, decision, status, sandbox, exit_code,
                    duration_ms, output_chars, output_preview, error,
                    error_code, project_id, user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    turn_id,
                    tool_call_id,
                    tool_name,
                    safe_arguments,
                    risk_level,
                    decision,
                    status,
                    sandbox,
                    exit_code,
                    duration_ms,
                    output_chars,
                    preview,
                    error,
                    error_code,
                    project_id or current_project_id(),
                    user_id or current_user_id(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # pragma: no cover - audit must never break a tool call
        log.exception("audit_write_failed", tool=tool_name, session_id=session_id)


def list_tool_calls(
    *,
    session_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    project_id: str | None = None,
    include_all: bool = False,
) -> list[dict[str, Any]]:
    """Return newest-first audit rows, scoped to a project by default."""
    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    clauses: list[str] = []
    params: list[Any] = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if not include_all:
        clauses.append("project_id = ?")
        params.append(project_id or current_project_id())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.extend([safe_limit, safe_offset])
    conn = database.get_db()
    try:
        rows = conn.execute(
            f"SELECT * FROM tool_audit{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def audit_stats(
    session_id: str | None = None,
    *,
    project_id: str | None = None,
    include_all: bool = False,
) -> list[dict[str, Any]]:
    """Aggregate decisions/statuses for a small dashboard."""
    clauses: list[str] = []
    params: list[Any] = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if not include_all:
        clauses.append("project_id = ?")
        params.append(project_id or current_project_id())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = database.get_db()
    try:
        rows = conn.execute(
            f"""
            SELECT risk_level, decision, status, COUNT(*) AS count
            FROM tool_audit{where}
            GROUP BY risk_level, decision, status
            ORDER BY count DESC
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def purge_audit(days: int) -> int:
    """Delete audit rows older than ``days``; returns affected row count."""
    days = max(1, int(days))
    conn = database.get_db()
    try:
        cursor = conn.execute(
            "DELETE FROM tool_audit WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def export_tool_calls(
    *,
    project_id: str | None = None,
    include_all: bool = False,
    limit: int = 10000,
) -> list[dict[str, Any]]:
    return list_tool_calls(
        limit=min(max(1, int(limit)), 10000),
        offset=0,
        project_id=project_id,
        include_all=include_all,
    )
