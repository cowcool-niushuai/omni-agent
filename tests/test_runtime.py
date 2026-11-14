"""Runtime tests for streaming, approval resume, checkpoints and compaction."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import database
import permission
import prompts
import tools
from omni_agent.runtime.context import maybe_compact
from omni_agent.runtime.graph import AgentRuntime


def text_chunk(text: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=text,
                    reasoning_content=None,
                    tool_calls=None,
                    model_extra={},
                )
            )
        ]
    )


def tool_chunk(call_id: str, name: str, arguments: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    reasoning_content=None,
                    model_extra={},
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            id=call_id,
                            function=SimpleNamespace(name=name, arguments=arguments),
                        )
                    ],
                )
            )
        ]
    )


class FakeStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self) -> "FakeStream":
        return self

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class FakeClient:
    """Minimal AsyncOpenAI stand-in with scripted streaming responses."""

    def __init__(self, scripts: list[list[Any]]) -> None:
        self.scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            if not self.scripts:
                return FakeStream([text_chunk("默认回复")])
            return FakeStream(self.scripts.pop(0))
        message = SimpleNamespace(
            content="压缩摘要",
            tool_calls=None,
            model_dump=lambda **_: {"role": "assistant", "content": "压缩摘要"},
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.fixture
def isolated_db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    monkeypatch.setattr(prompts, "build_system_prompt", lambda: "system")
    monkeypatch.setattr(tools, "get_all_tool_definitions", lambda: [])
    yield tmp_path


def make_runtime(tmp_path: Any, client: FakeClient) -> AgentRuntime:
    return AgentRuntime(
        checkpoint_path=tmp_path / "checkpoints.db",
        client_factory=lambda: (client, "fake-model"),
    )


async def test_streams_real_tokens_and_persists(isolated_db: Any) -> None:
    client = FakeClient([[text_chunk("你"), text_chunk("好")]])
    runtime = make_runtime(isolated_db, client)
    try:
        events = [event async for event in runtime.run_stream("s1", "hi")]
    finally:
        await runtime.close()

    chunks = [event["content"] for event in events if event["type"] == "answer_chunk"]
    assert chunks == ["你", "好"]
    assert events[-1]["type"] == "done"
    assert client.calls[0]["stream"] is True

    messages = database.get_session_messages("s1")
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == "你好"


async def test_approval_interrupt_then_resume_executes_tool(
    isolated_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = "rm -rf /tmp/omni-runtime-test"
    client = FakeClient(
        [
            [tool_chunk("call_1", "bash_run", json.dumps({"command": command}))],
            [text_chunk("清理完成")],
        ]
    )
    executed: list[tuple[str, dict[str, Any]]] = []

    def fake_dispatch(name: str, arguments: dict[str, Any]) -> str:
        executed.append((name, arguments))
        return "工具已执行"

    monkeypatch.setattr(tools, "dispatch_tool", fake_dispatch)
    runtime = make_runtime(isolated_db, client)
    try:
        events = [event async for event in runtime.run_stream("s1", "清理临时文件")]
        permission_event = next(
            event for event in events if event["type"] == "permission_intercept"
        )
        assert permission_event["requests"][0]["tool_name"] == "bash_run"
        assert not any(event["type"] == "done" for event in events)
        request_id = permission_event["request_id"]
        tool_request_id = permission_event["requests"][0]["request_id"]
        assert permission.get_request_status(tool_request_id) == "PENDING"

        resumed = [
            event
            async for event in runtime.resume_stream("s1", request_id, "APPROVE")
        ]
    finally:
        await runtime.close()

    assert any(
        event["type"] == "tool_result" and "工具已执行" in event["result"]
        for event in resumed
    )
    assert resumed[-1]["type"] == "done"
    assert executed == [("bash_run", {"command": command})]
    assert permission.get_request_status(tool_request_id) == "APPROVED"


async def test_rejected_approval_does_not_execute_tool(
    isolated_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient(
        [
            [tool_chunk("call_1", "bash_run", json.dumps({"command": "rm -rf /tmp/x"}))],
            [text_chunk("已取消")],
        ]
    )
    executed: list[str] = []
    monkeypatch.setattr(
        tools,
        "dispatch_tool",
        lambda name, arguments: executed.append(name) or "不应执行",
    )
    runtime = make_runtime(isolated_db, client)
    try:
        events = [event async for event in runtime.run_stream("s1", "执行危险命令")]
        permission_event = next(
            event for event in events if event["type"] == "permission_intercept"
        )
        request_id = permission_event["request_id"]
        tool_request_id = permission_event["requests"][0]["request_id"]
        resumed = [
            event
            async for event in runtime.resume_stream("s1", request_id, "REJECT")
        ]
    finally:
        await runtime.close()

    assert executed == []
    assert any(
        event["type"] == "tool_result" and "用户拒绝" in event["result"]
        for event in resumed
    )
    assert resumed[-1]["type"] == "done"
    assert permission.get_request_status(tool_request_id) == "REJECTED"


async def test_checkpoint_survives_runtime_restart(isolated_db: Any) -> None:
    client = FakeClient([[text_chunk("持久化回复")]])
    runtime = make_runtime(isolated_db, client)
    try:
        _ = [event async for event in runtime.run_stream("s1", "请记住")]
    finally:
        await runtime.close()

    runtime2 = make_runtime(isolated_db, FakeClient([]))
    try:
        graph = await runtime2._get_graph()
        state = await graph.aget_state({"configurable": {"thread_id": "s1"}})
    finally:
        await runtime2.close()

    assert state.values["messages"]
    assert state.values["final_answer"] == "持久化回复"


async def test_compaction_drops_old_messages(isolated_db: Any) -> None:
    messages = [{"role": "user", "content": "x" * 300} for _ in range(20)]
    client = FakeClient([])
    summary, cut, dropped = await maybe_compact(
        messages=messages,
        summary=None,
        summary_upto=0,
        client=client,
        model="fake-model",
        max_tokens=200,
        keep_last=4,
    )

    assert dropped == 16
    assert cut == 16
    assert summary == "压缩摘要"
    assert client.calls and client.calls[0]["stream"] is False
