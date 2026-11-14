"""Audit trail tests."""

from __future__ import annotations

from typing import Any

import pytest

import audit
import database


@pytest.fixture
def isolated_db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "audit.db"))
    database.init_db()
    return tmp_path


def test_record_and_list(isolated_db: Any) -> None:
    audit.record_tool_call(
        session_id="s1",
        turn_id="t1",
        tool_call_id="call_1",
        tool_name="bash_run",
        arguments={"command": "ls", "api_key": "secret-value"},
        risk_level="ALLOW",
        decision="AUTO",
        status="OK",
        sandbox="process",
        exit_code=0,
        duration_ms=12,
        output_chars=2,
        output_preview="ok",
    )
    rows = audit.list_tool_calls(session_id="s1")
    assert len(rows) == 1
    row = rows[0]
    assert row["tool_name"] == "bash_run"
    assert row["status"] == "OK"
    assert "secret-value" not in row["arguments"]
    assert "REDACTED" in row["arguments"]


def test_stats_and_filtering(isolated_db: Any) -> None:
    for index, risk in enumerate(["ALLOW", "ASK_USER", "DENY"]):
        audit.record_tool_call(
            session_id="s1" if index < 2 else "s2",
            turn_id="t1",
            tool_call_id=f"call_{index}",
            tool_name="bash_run",
            arguments={},
            risk_level=risk,
            decision="APPROVED" if risk == "ASK_USER" else "AUTO",
            status="BLOCKED" if risk == "DENY" else "OK",
        )
    assert len(audit.list_tool_calls(session_id="s1")) == 2
    stats = audit.audit_stats()
    assert sum(int(item["count"]) for item in stats) == 3


def test_audit_never_raises_on_missing_table(isolated_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(database, "get_db", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    # Must swallow the error rather than break the tool path.
    audit.record_tool_call(
        session_id="s1",
        turn_id=None,
        tool_call_id=None,
        tool_name="bash_run",
        arguments={},
        risk_level="ALLOW",
        decision="AUTO",
        status="OK",
    )
