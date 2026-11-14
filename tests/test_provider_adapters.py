import json
from types import SimpleNamespace
from typing import Any

import pytest

from provider_adapters import (
    AnthropicAdapter,
    OpenAICompatibleAdapter,
    anthropic_messages,
    anthropic_tool_choice,
    anthropic_tools,
    build_adapter,
    normalize_adapter,
)


def test_normalize_adapter_aliases() -> None:
    assert normalize_adapter("openai") == "openai-compatible"
    assert normalize_adapter("gemini") == "openai-compatible"
    assert normalize_adapter("", "anthropic") == "anthropic"
    with pytest.raises(ValueError):
        normalize_adapter("unknown-vendor")


def test_anthropic_messages_convert_tool_calls_and_results() -> None:
    system, messages = anthropic_messages(
        [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "列出文件"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "file_list",
                            "arguments": json.dumps({"path": "."}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "a.txt\nb.txt",
            },
            {"role": "user", "content": "继续"},
        ]
    )

    assert system == "你是助手"
    assert messages[0] == {"role": "user", "content": [{"type": "text", "text": "列出文件"}]}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"][0]["type"] == "tool_use"
    assert messages[1]["content"][0]["input"] == {"path": "."}
    assert messages[2]["role"] == "user"
    assert messages[2]["content"][0]["type"] == "tool_result"
    assert messages[2]["content"][1] == {"type": "text", "text": "继续"}


def test_anthropic_tools_and_choice() -> None:
    tools = anthropic_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "bash_run",
                    "description": "运行命令",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                    },
                },
            }
        ]
    )
    assert tools[0]["name"] == "bash_run"
    assert tools[0]["input_schema"]["properties"]["command"]["type"] == "string"
    assert anthropic_tool_choice("auto") == {"type": "auto"}
    assert anthropic_tool_choice("required") == {"type": "any"}
    assert anthropic_tool_choice({"function": {"name": "bash_run"}}) == {
        "type": "tool",
        "name": "bash_run",
    }


def test_build_adapter_selects_vendor() -> None:
    openai_adapter = build_adapter(
        provider_key="deepseek",
        provider={"adapter": "openai-compatible", "base_url": "https://api.deepseek.com"},
        api_key="test",
    )
    anthropic_adapter = build_adapter(
        provider_key="anthropic",
        provider={"adapter": "anthropic", "base_url": "https://api.anthropic.com/v1"},
        api_key="test",
    )
    assert isinstance(openai_adapter, OpenAICompatibleAdapter)
    assert isinstance(anthropic_adapter, AnthropicAdapter)
    assert anthropic_adapter.client.base_url.host == "api.anthropic.com"


class FakeMessages:
    def __init__(self, *, stream_events: list[Any] | None = None, response: Any = None) -> None:
        self.stream_events = stream_events or []
        self.response = response

    async def create(self, **_: Any) -> Any:
        return self.response

    def stream(self, **_: Any) -> Any:
        return FakeStreamContext(self.stream_events)


class FakeStreamContext:
    def __init__(self, events: list[Any]) -> None:
        self.events = events

    async def __aenter__(self) -> "FakeStreamContext":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def __aiter__(self) -> Any:
        for event in self.events:
            yield event


def make_anthropic_adapter() -> AnthropicAdapter:
    return AnthropicAdapter(
        provider_key="anthropic",
        provider={"adapter": "anthropic", "base_url": "https://api.anthropic.com"},
        api_key="test",
        model_info={"limit": {"output": 1024}},
    )


async def test_anthropic_non_stream_tool_response() -> None:
    adapter = make_anthropic_adapter()
    fake_messages = FakeMessages(
        response=SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="我来执行"),
                SimpleNamespace(
                    type="tool_use",
                    id="toolu_1",
                    name="bash_run",
                    input={"command": "ls"},
                ),
            ]
        )
    )
    adapter.client = SimpleNamespace(messages=fake_messages)

    response = await adapter.create(
        model="claude-sonnet",
        messages=[{"role": "user", "content": "列出文件"}],
        stream=False,
    )
    message = response.choices[0].message
    assert message.content == "我来执行"
    assert message.tool_calls[0].id == "toolu_1"
    dumped = message.model_dump()
    assert dumped["tool_calls"][0]["function"]["name"] == "bash_run"
    assert json.loads(dumped["tool_calls"][0]["function"]["arguments"]) == {"command": "ls"}


async def test_anthropic_stream_translates_text_and_tool_calls() -> None:
    adapter = make_anthropic_adapter()
    events = [
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="text", text=""),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text="你好"),
        ),
        SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(
                type="tool_use", id="toolu_1", name="bash_run", input={}
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"command":'),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(type="input_json_delta", partial_json='"ls"}'),
        ),
        SimpleNamespace(type="message_stop"),
    ]
    adapter.client = SimpleNamespace(messages=FakeMessages(stream_events=events))

    stream = await adapter.create(
        model="claude-sonnet",
        messages=[{"role": "user", "content": "执行"}],
        stream=True,
    )
    text = ""
    tool_arguments = ""
    tool_name = ""
    tool_id = ""
    async for chunk in stream:
        delta = chunk.choices[0].delta
        if delta.content:
            text += delta.content
        for call in delta.tool_calls or []:
            tool_name += call.function.name or ""
            tool_arguments += call.function.arguments or ""
            tool_id = call.id or tool_id

    assert text == "你好"
    assert tool_name == "bash_run"
    assert tool_id == "toolu_1"
    assert json.loads(tool_arguments) == {"command": "ls"}


def test_anthropic_messages_converts_image_blocks() -> None:
    system, messages = anthropic_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "描述图片"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        ]
    )
    assert system == ""
    assert messages == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "描述图片"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "AAAA",
                    },
                },
            ],
        }
    ]
