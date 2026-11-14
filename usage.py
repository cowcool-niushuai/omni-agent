"""Token usage accounting for model responses.

Providers expose usage under different names.  This module normalizes the
common OpenAI/DeepSeek and Anthropic shapes, persists one row per model call,
and produces aggregates for the statistics screens.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

from database import get_db


def _as_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump())
        except Exception:
            pass
    return dict(getattr(value, "__dict__", {}) or {})


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_usage(raw: Any) -> Optional[Dict[str, int]]:
    """Normalize provider usage into token counters.

    Returns ``None`` when the provider did not return any usable usage data.
    """
    data = _as_dict(raw)
    if not data:
        return None
    prompt_details = _as_dict(
        data.get("prompt_tokens_details") or data.get("input_tokens_details")
    )
    completion_details = _as_dict(
        data.get("completion_tokens_details") or data.get("output_tokens_details")
    )
    input_tokens = _int(data.get("prompt_tokens") or data.get("input_tokens"))
    output_tokens = _int(data.get("completion_tokens") or data.get("output_tokens"))
    reasoning_tokens = _int(
        data.get("reasoning_tokens")
        or completion_details.get("reasoning_tokens")
        or data.get("thoughts_tokens")
    )
    cache_read = _int(
        prompt_details.get("cached_tokens")
        or data.get("cache_read_input_tokens")
        or data.get("prompt_cache_hit_tokens")
    )
    cache_write = _int(
        data.get("cache_creation_input_tokens")
        or data.get("cache_write_tokens")
        or data.get("prompt_cache_miss_tokens")
    )
    if not any((input_tokens, output_tokens, reasoning_tokens, cache_read, cache_write)):
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }


def compute_cost(tokens: Dict[str, Any], pricing: Dict[str, Any]) -> float:
    """Estimate USD cost from token counts and per-million-token prices.

    Unknown or missing pricing returns 0 instead of inventing a number.
    Cache writes fall back to the normal input price when no separate rate is
    configured, which matches how most providers bill cache creation.
    """
    if not pricing:
        return 0.0

    def per_million(name: str, fallback: float = 0.0) -> float:
        try:
            value = float(pricing.get(name, fallback) or fallback)
        except (TypeError, ValueError):
            return fallback
        return max(0.0, value) / 1_000_000.0

    input_tokens = _int(tokens.get("input_tokens"))
    output_tokens = _int(tokens.get("output_tokens"))
    cache_read = min(_int(tokens.get("cache_read_tokens")), input_tokens)
    cache_write = _int(tokens.get("cache_write_tokens"))
    uncached = max(0, input_tokens - cache_read)

    input_price = per_million("input")
    output_price = per_million("output")
    read_price = per_million("cache_read", pricing.get("input", 0.0))
    write_price = per_million("cache_write", pricing.get("input", 0.0))
    cost = (
        uncached * input_price
        + cache_read * read_price
        + output_tokens * output_price
        + cache_write * write_price
    )
    return round(cost, 8)


def ensure_table() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_events(session_id, id)"
        )


def record_usage(
    session_id: str,
    model: str,
    tokens: Dict[str, int],
    *,
    cost: float = 0.0,
) -> None:
    if not session_id or not tokens:
        return
    ensure_table()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO usage_events
                (session_id, model, input_tokens, output_tokens, reasoning_tokens,
                 cache_read_tokens, cache_write_tokens, cost)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                model or "",
                int(tokens.get("input_tokens", 0)),
                int(tokens.get("output_tokens", 0)),
                int(tokens.get("reasoning_tokens", 0)),
                int(tokens.get("cache_read_tokens", 0)),
                int(tokens.get("cache_write_tokens", 0)),
                float(cost or 0.0),
            ),
        )


def _empty_total() -> Dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost": 0.0,
        "calls": 0,
    }


def usage_summary(session_id: Optional[str] = None) -> Dict[str, Any]:
    ensure_table()
    where = " WHERE session_id = ?" if session_id else ""
    params: tuple[Any, ...] = (session_id,) if session_id else ()
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                   COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                   COALESCE(SUM(cost), 0) AS cost
            FROM usage_events{where}
            """,
            params,
        ).fetchone()
        recent = conn.execute(
            f"""
            SELECT model, input_tokens, output_tokens, reasoning_tokens,
                   cache_read_tokens, cache_write_tokens, cost, created_at
            FROM usage_events{where}
            ORDER BY id DESC LIMIT 1
            """,
            params,
        ).fetchone()
    total = dict(row) if row else _empty_total()
    total["calls"] = int(total.get("calls") or 0)
    for key in (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ):
        total[key] = int(total.get(key) or 0)
    total["cost"] = float(total.get("cost") or 0.0)
    input_tokens = total["input_tokens"]
    total["cache_hit_rate"] = (
        round(total["cache_read_tokens"] / input_tokens * 100, 1)
        if input_tokens
        else 0.0
    )
    recent_data = dict(recent) if recent else None
    if recent_data:
        for key in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            recent_data[key] = int(recent_data.get(key) or 0)
        recent_data["cost"] = float(recent_data.get("cost") or 0.0)
        recent_data["cache_hit_rate"] = (
            round(recent_data["cache_read_tokens"] / recent_data["input_tokens"] * 100, 1)
            if recent_data["input_tokens"]
            else 0.0
        )
    return {"total": total, "recent": recent_data}


def clear_usage(session_id: Optional[str] = None) -> int:
    ensure_table()
    with get_db() as conn:
        if session_id:
            cur = conn.execute("DELETE FROM usage_events WHERE session_id = ?", (session_id,))
        else:
            cur = conn.execute("DELETE FROM usage_events")
        return int(cur.rowcount or 0)


def _unused_sqlite_guard() -> None:  # pragma: no cover - keeps sqlite3 import explicit
    _ = sqlite3
