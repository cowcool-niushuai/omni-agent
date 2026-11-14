from pathlib import Path

import pytest

import database


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "sessions.db"))
    database.init_db()
    return tmp_path


def test_session_list_enriches_and_searches(isolated_db: Path) -> None:
    database.create_session("s1", "新智能体对话")
    database.save_message("s1", "user", "帮我分析 Kubernetes 部署")
    database.save_message("s1", "assistant", "好的")
    database.create_session("s2", "另一个任务")
    database.save_message("s2", "user", "写一个脚本")

    sessions = database.list_sessions()
    assert {item["id"] for item in sessions} == {"s1", "s2"}
    first = next(item for item in sessions if item["id"] == "s1")
    assert first["message_count"] == 2
    assert first["last_role"] == "assistant"
    assert first["last_message"] == "好的"

    found = database.list_sessions(query="Kubernetes")
    assert [item["id"] for item in found] == ["s1"]


def test_auto_title_only_for_default_title(isolated_db: Path) -> None:
    database.create_session("s1")
    database.save_message("s1", "user", "帮我写一个 Python 排序脚本并运行测试")
    database.maybe_auto_title("s1", "帮我写一个 Python 排序脚本并运行测试")
    session = database.get_session("s1")
    assert session is not None
    assert session["title"].startswith("帮我写一个 Python")

    database.create_session("s2", "手动标题")
    database.save_message("s2", "user", "另一条消息")
    database.maybe_auto_title("s2", "另一条消息")
    assert database.get_session("s2")["title"] == "手动标题"


def test_rename_clear_and_delete_session(isolated_db: Path) -> None:
    database.create_session("s1")
    database.save_message("s1", "user", "hello")
    database.save_message("s1", "assistant", "world")

    roles = database.get_database_stats()["message_roles"]
    assert roles.get("user") == 1
    assert roles.get("assistant") == 1

    renamed = database.rename_session("s1", "  新标题  ")
    assert renamed is not None
    assert renamed["title"] == "新标题"

    assert database.clear_session_messages("s1")
    assert database.get_session_messages("s1") == []
    assert database.get_session("s1") is not None

    assert database.delete_session("s1")
    assert database.get_session("s1") is None
    assert not database.delete_session("missing")

    stats = database.get_database_stats()
    assert stats["counts"]["sessions"] == 0
    assert stats["message_roles"].get("user", 0) == 0
    assert stats["database_bytes"] >= 0
