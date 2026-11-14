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
                    duration_ms, output_chars, output_preview, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
) -> list[dict[str, Any]]:
    """Return newest-first audit rows."""
    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    conn = database.get_db()
    try:
        if session_id:
            rows = conn.execute(
                """
                SELECT * FROM tool_audit
                WHERE session_id = ?
                ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (session_id, safe_limit, safe_offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tool_audit ORDER BY id DESC LIMIT ? OFFSET ?",
                (safe_limit, safe_offset),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def audit_stats(session_id: str | None = None) -> list[dict[str, Any]]:
    """Aggregate decisions/statuses for a small dashboard."""
    conn = database.get_db()
    try:
        if session_id:
            rows = conn.execute(
                """
                SELECT risk_level, decision, status, COUNT(*) AS count
                FROM tool_audit WHERE session_id = ?
                GROUP BY risk_level, decision, status
                ORDER BY count DESC
                """,
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT risk_level, decision, status, COUNT(*) AS count
                FROM tool_audit
                GROUP BY risk_level, decision, status
                ORDER BY count DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
