"""Scheduled task persistence and unattended execution tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator

import pytest

import database
from scheduler import Scheduler


@pytest.fixture
def isolated_db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    yield tmp_path


class FakeRuntime:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.calls: list[dict[str, Any]] = []

    async def run_stream(
        self,
        session_id: str,
        user_message: str,
        *,
        max_iterations: int | None = None,
        approval_mode: str = "human",
    ) -> AsyncGenerator[dict[str, Any], None]:
        self.calls.append(
            {
                "session_id": session_id,
                "user_message": user_message,
                "approval_mode": approval_mode,
            }
        )
        for event in self.events:
            yield event


def test_scheduled_task_crud_and_due(isolated_db: Any) -> None:
    task = database.create_scheduled_task("晨间简报", "总结昨天的日志", 120)
    task_id = str(task["id"])
    assert task["enabled"] is True
    assert task["last_status"] == "NEVER"
    assert str(task["session_id"]).startswith("sched_")

    updated = database.update_scheduled_task(
        task_id, name="每日简报", interval_seconds=300, enabled=False
    )
    assert updated is not None
    assert updated["name"] == "每日简报"
    assert updated["interval_seconds"] == 300
    assert updated["enabled"] is False

    future = datetime.now(timezone.utc) + timedelta(seconds=301)
    assert database.due_scheduled_tasks(now=future) == []

    database.update_scheduled_task(task_id, enabled=True)
    due = database.due_scheduled_tasks(now=future)
    assert [item["id"] for item in due] == [task_id]

    finished = database.finish_scheduled_task_run(task_id, "OK", "完成")
    assert finished is not None
    assert finished["last_status"] == "OK"
    assert finished["last_result"] == "完成"
    assert finished["run_count"] == 1
    assert database.delete_scheduled_task(task_id) is True
    assert database.get_scheduled_task(task_id) is None


async def test_scheduler_executes_with_auto_reject(isolated_db: Any) -> None:
    task = database.create_scheduled_task("日志巡检", "检查最近错误", 60)
    task_id = str(task["id"])
    fake_runtime = FakeRuntime(
        [
            {"type": "answer_chunk", "content": "巡检完成，"},
            {"type": "answer_chunk", "content": "没有发现异常。"},
            {"type": "done"},
        ]
    )
    scheduler = Scheduler(fake_runtime, poll_seconds=0.01)

    result = await scheduler.execute(task_id)

    assert result["started"] is True
    assert result["status"] == "OK"
    assert fake_runtime.calls[0]["approval_mode"] == "auto_reject"
    stored = database.get_scheduled_task(task_id)
    assert stored is not None
    assert stored["last_status"] == "OK"
    assert "巡检完成" in stored["last_result"]


async def test_scheduler_marks_auto_rejected_run_blocked(isolated_db: Any) -> None:
    task = database.create_scheduled_task("危险任务", "删除临时目录", 60)
    task_id = str(task["id"])
    fake_runtime = FakeRuntime(
        [
            {
                "type": "tool_result",
                "tool_call_id": "call_1",
                "name": "bash_run",
                "result": "[自动拒绝] 该高危操作未获批准：需要人工审批",
            },
            {"type": "answer_chunk", "content": "已跳过高危操作。"},
            {"type": "done"},
        ]
    )
    scheduler = Scheduler(fake_runtime, poll_seconds=0.01)

    result = await scheduler.execute(task_id)

    assert result["status"] == "BLOCKED"
    stored = database.get_scheduled_task(task_id)
    assert stored is not None
    assert stored["last_status"] == "BLOCKED"
    assert "自动拒绝" in stored["last_result"]


async def test_scheduler_recovers_interrupted_running_state(isolated_db: Any) -> None:
    task = database.create_scheduled_task("恢复测试", "继续执行", 60)
    task_id = str(task["id"])
    database.mark_scheduled_task_running(task_id)
    assert database.get_scheduled_task(task_id)["last_status"] == "RUNNING"

    assert database.recover_scheduled_tasks() == 1
    recovered = database.get_scheduled_task(task_id)
    assert recovered is not None
    assert recovered["last_status"] == "INTERRUPTED"
