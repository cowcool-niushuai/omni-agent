from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

import usage


@pytest.fixture()
def usage_db(tmp_path, monkeypatch):
    path = tmp_path / "usage.db"

    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(usage, "get_db", connect)
    usage.ensure_table()
    return path


def test_normalize_openai_and_deepseek_usage():
    raw = SimpleNamespace(
        prompt_tokens=120,
        completion_tokens=30,
        prompt_tokens_details=SimpleNamespace(cached_tokens=80),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
    )
    result = usage.normalize_usage(raw)
    assert result == {
        "input_tokens": 120,
        "output_tokens": 30,
        "reasoning_tokens": 12,
        "cache_read_tokens": 80,
        "cache_write_tokens": 0,
    }

    deepseek = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "prompt_cache_hit_tokens": 60,
        "prompt_cache_miss_tokens": 40,
    }
    result = usage.normalize_usage(deepseek)
    assert result is not None
    assert result["cache_read_tokens"] == 60


def test_normalize_anthropic_usage():
    raw = {
        "input_tokens": 200,
        "output_tokens": 40,
        "cache_read_input_tokens": 150,
        "cache_creation_input_tokens": 10,
    }
    result = usage.normalize_usage(raw)
    assert result is not None
    assert result["input_tokens"] == 200
    assert result["cache_read_tokens"] == 150
    assert result["cache_write_tokens"] == 10


def test_normalize_zero_usage_is_ignored():
    assert usage.normalize_usage({"prompt_tokens": 0, "completion_tokens": 0}) is None


def test_record_and_summarize_usage(usage_db):
    usage.record_usage(
        "sess_a",
        "deepseek/deepseek-v4-pro",
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "reasoning_tokens": 5,
            "cache_read_tokens": 50,
            "cache_write_tokens": 0,
        },
    )
    usage.record_usage(
        "sess_a",
        "deepseek/deepseek-v4-pro",
        {"input_tokens": 300, "output_tokens": 60, "cache_read_tokens": 150},
    )
    usage.record_usage(
        "sess_b",
        "deepseek/deepseek-v4-pro",
        {"input_tokens": 999, "output_tokens": 1},
    )

    summary = usage.usage_summary("sess_a")
    total = summary["total"]
    assert total["calls"] == 2
    assert total["input_tokens"] == 400
    assert total["output_tokens"] == 80
    assert total["reasoning_tokens"] == 5
    assert total["cache_read_tokens"] == 200
    assert total["cache_hit_rate"] == 50.0
    assert summary["recent"]["input_tokens"] == 300

    global_summary = usage.usage_summary()
    assert global_summary["total"]["calls"] == 3
    assert global_summary["total"]["input_tokens"] == 1399


def test_clear_usage(usage_db):
    usage.record_usage("sess_a", "model", {"input_tokens": 10, "output_tokens": 2})
    usage.clear_usage()
    assert usage.usage_summary()["total"]["calls"] == 0


def test_compute_cost_with_pricing() -> None:
    tokens = {
        "input_tokens": 1_000_000,
        "cache_read_tokens": 400_000,
        "cache_write_tokens": 100_000,
        "output_tokens": 200_000,
    }
    pricing = {
        "input": 0.28,
        "output": 0.42,
        "cache_read": 0.028,
        "cache_write": 0.28,
    }
    cost = usage.compute_cost(tokens, pricing)
    expected = 0.6 * 0.28 + 0.4 * 0.028 + 0.1 * 0.28 + 0.2 * 0.42
    assert abs(cost - expected) < 1e-9


def test_compute_cost_without_pricing_is_zero() -> None:
    assert usage.compute_cost({"input_tokens": 100}, {}) == 0.0
    assert usage.compute_cost({"input_tokens": 100}, None) == 0.0


def test_compute_cost_cache_read_falls_back_to_input_rate() -> None:
    tokens = {"input_tokens": 1_000_000, "cache_read_tokens": 1_000_000}
    cost = usage.compute_cost(tokens, {"input": 1.0})
    assert abs(cost - 1.0) < 1e-9
