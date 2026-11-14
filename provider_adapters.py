"""Provider adapters for the agent runtime.

The runtime is written against the OpenAI chat-completions shape.  Adapters
translate vendor-native APIs into that shape so the graph, permission layer
and audit trail do not need vendor-specific branches.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

SUPPORTED_ADAPTERS = {
    "openai-compatible",
    "anthropic",
}

_ADAPTER_ALIASES = {
    "openai": "openai-compatible",
    "openai_compatible": "openai-compatible",
    "custom": "openai-compatible",
    # Gemini, Ollama, LM Studio and vLLM all expose OpenAI-compatible
    # endpoints; they use the same adapter and are distinguished by provider.
    "gemini": "openai-compatible",
    "google": "openai-compatible",
    "google-gemini": "openai-compatible",
    "ollama": "openai-compatible",
    "local": "openai-compatible",
    "lmstudio": "openai-compatible",
    "vllm": "openai-compatible",
}

DEFAULT_ANTHROPIC_MAX_TOKENS = 4096


def normalize_adapter(value: str | None, provider_key: str = "") -> str:
    """Return a supported adapter name, inferring the vendor when omitted."""
    candidate = (value or "").strip().lower()
    if not candidate:
        candidate = "anthropic" if provider_key == "anthropic" else "openai-compatible"
    candidate = _ADAPTER_ALIASES.get(candidate, candidate)
    if candidate not in SUPPORTED_ADAPTERS:
        raise ValueError(f"不支持的模型适配器: {value}")
    return candidate


def _chunk(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    tool_calls: list[Any] | None = None,
) -> Any:
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        model_extra={},
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _tool_delta(
    *,
    index: int,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> Any:
    return SimpleNamespace(
        index=index,
        id=call_id,
        type="function" if call_id else None,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _as_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {"text", "input_text"}:
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return str(content)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _append_block(messages: list[dict[str, Any]], role: str, block: dict[str, Any]) -> None:
    """Append a content block, merging consecutive same-role messages."""
    if messages and messages[-1].get("role") == role:
        messages[-1]["content"].append(block)
        return
    messages.append({"role": role, "content": [block]})


def anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Convert OpenAI-shaped messages to Anthropic messages plus system text."""
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []

    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role == "system":
            text = _as_text(content)
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            _append_block(
                converted,
                "user",
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": _as_text(content),
                },
            )
            continue

        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = _as_text(content)
            if text:
                blocks.append({"type": "text", "text": text})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    function = getattr(call, "function", None)
                    call = {
                        "id": getattr(call, "id", ""),
                        "function": {
                            "name": getattr(function, "name", ""),
                            "arguments": getattr(function, "arguments", "{}"),
                        },
                    }
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                if not name:
                    continue
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or f"toolu_{len(blocks)}"),
                        "name": name,
                        "input": _json_object(function.get("arguments")),
                    }
                )
            if not blocks:
                continue
            if converted and converted[-1].get("role") == "assistant":
                converted[-1]["content"].extend(blocks)
            else:
                converted.append({"role": "assistant", "content": blocks})
            continue

        text = _as_text(content)
        _append_block(converted, "user", {"type": "text", "text": text})

    if not converted:
        converted.append({"role": "user", "content": [{"type": "text", "text": ""}]})
    return "\n\n".join(system_parts), converted


def anthropic_tools(tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI function schemas to Anthropic tool definitions."""
    tools: list[dict[str, Any]] = []
    for item in tool_schemas:
        function = item.get("function") if isinstance(item, dict) else None
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "")
        if not name:
            continue
        parameters = function.get("parameters")
        if isinstance(parameters, str):
            parameters = _json_object(parameters)
        if not isinstance(parameters, dict) or not parameters:
            parameters = {"type": "object", "properties": {}}
        tools.append(
            {
                "name": name,
                "description": str(function.get("description") or ""),
                "input_schema": parameters,
            }
        )
    return tools


def anthropic_tool_choice(value: Any) -> dict[str, Any] | None:
    if value in (None, "", "none"):
        return None
    if value == "auto":
        return {"type": "auto"}
    if value == "required":
        return {"type": "any"}
    if isinstance(value, dict):
        function = value.get("function") or {}
        name = function.get("name") if isinstance(function, dict) else None
        if name:
            return {"type": "tool", "name": str(name)}
    return {"type": "auto"}


class OpenAICompatibleAdapter:
    """OpenAI, DeepSeek, Qwen, Kimi, Gemini-compatible, Ollama, vLLM, ..."""

    kind = "openai-compatible"

    def __init__(
        self,
        *,
        provider_key: str,
        provider: dict[str, Any],
        api_key: str,
    ) -> None:
        self.provider_key = provider_key
        self.provider = provider
        self.api_key = api_key
        headers = provider.get("headers") if isinstance(provider.get("headers"), dict) else None
        self.client = AsyncOpenAI(
            api_key=api_key or "not-needed",
            base_url=str(provider.get("base_url") or "https://api.openai.com/v1"),
            timeout=120.0,
            default_headers=headers,
        )
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    async def create(self, **kwargs: Any) -> Any:
        body = self.provider.get("body")
        if isinstance(body, dict):
            for key, value in body.items():
                kwargs.setdefault(key, value)
        return await self.client.chat.completions.create(**kwargs)

    async def list_models(self) -> list[dict[str, Any]]:
        response = await self.client.models.list()
        result: list[dict[str, Any]] = []
        for model in response.data:
            model_id = str(getattr(model, "id", "") or "")
            if model_id:
                result.append(
                    {
                        "id": model_id,
                        "name": str(getattr(model, "name", "") or model_id),
                    }
                )
        return result

    async def test_connection(self) -> dict[str, Any]:
        started = time.perf_counter()
        models = await self.list_models()
        return {
            "ok": True,
            "adapter": self.kind,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "models": [item["id"] for item in models],
        }

    async def close(self) -> None:
        await self.client.close()


class AnthropicAdapter:
    """Native Anthropic Messages API adapter with tool-use support."""

    kind = "anthropic"

    def __init__(
        self,
        *,
        provider_key: str,
        provider: dict[str, Any],
        api_key: str,
        model_info: dict[str, Any] | None = None,
    ) -> None:
        self.provider_key = provider_key
        self.provider = provider
        self.api_key = api_key
        self.model_info = model_info or {}
        base_url = str(provider.get("base_url") or "https://api.anthropic.com").rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        headers = provider.get("headers") if isinstance(provider.get("headers"), dict) else None
        self.client = AsyncAnthropic(
            api_key=api_key or "not-needed",
            base_url=base_url,
            timeout=120.0,
            default_headers=headers,
        )
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    def _request(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        system, messages = anthropic_messages(kwargs.get("messages") or [])
        request: dict[str, Any] = {
            "model": str(kwargs.get("model") or ""),
            "messages": messages,
            "max_tokens": int(
                kwargs.get("max_tokens")
                or (self.model_info.get("limit") or {}).get("output")
                or DEFAULT_ANTHROPIC_MAX_TOKENS
            ),
        }
        if system:
            request["system"] = system
        if kwargs.get("temperature") is not None:
            request["temperature"] = kwargs["temperature"]
        tools = anthropic_tools(kwargs.get("tools") or [])
        if tools:
            request["tools"] = tools
            choice = anthropic_tool_choice(kwargs.get("tool_choice"))
            if choice:
                request["tool_choice"] = choice
        body = self.provider.get("body")
        if isinstance(body, dict):
            for key, value in body.items():
                request.setdefault(key, value)
        return request

    async def create(self, **kwargs: Any) -> Any:
        request = self._request(kwargs)
        if kwargs.get("stream"):
            return self._stream(request)
        return await self._complete(request)

    async def _complete(self, request: dict[str, Any]) -> Any:
        message = await self.client.messages.create(**request)
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[Any] = []
        for block in getattr(message, "content", []) or []:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                text_parts.append(str(getattr(block, "text", "") or ""))
            elif block_type == "thinking":
                reasoning_parts.append(str(getattr(block, "thinking", "") or ""))
            elif block_type == "tool_use":
                tool_calls.append(
                    SimpleNamespace(
                        id=str(getattr(block, "id", "") or ""),
                        type="function",
                        function=SimpleNamespace(
                            name=str(getattr(block, "name", "") or ""),
                            arguments=json.dumps(
                                getattr(block, "input", {}) or {}, ensure_ascii=False
                            ),
                        ),
                    )
                )

        content = "".join(text_parts)
        reasoning = "".join(reasoning_parts)
        message_namespace = SimpleNamespace(
            content=content,
            reasoning_content=reasoning or None,
            tool_calls=tool_calls or None,
        )

        def model_dump(**_: Any) -> dict[str, Any]:
            dumped: dict[str, Any] = {"role": "assistant", "content": content}
            if reasoning:
                dumped["reasoning_content"] = reasoning
            if tool_calls:
                dumped["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in tool_calls
                ]
            return dumped

        message_namespace.model_dump = model_dump
        return SimpleNamespace(choices=[SimpleNamespace(message=message_namespace)])

    async def _stream(self, request: dict[str, Any]) -> AsyncIterator[Any]:
        tool_indices: dict[int, int] = {}
        full_input_blocks: set[int] = set()
        async with self.client.messages.stream(**request) as stream:
            async for event in stream:
                event_type = getattr(event, "type", "")
                index = int(getattr(event, "index", 0) or 0)
                if event_type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    block_type = getattr(block, "type", "")
                    if block_type == "text":
                        text = str(getattr(block, "text", "") or "")
                        if text:
                            yield _chunk(content=text)
                    elif block_type == "tool_use":
                        tool_index = len(tool_indices)
                        tool_indices[index] = tool_index
                        initial_input = getattr(block, "input", None)
                        initial_arguments = ""
                        if initial_input:
                            initial_arguments = json.dumps(initial_input, ensure_ascii=False)
                            full_input_blocks.add(index)
                        yield _chunk(
                            tool_calls=[
                                _tool_delta(
                                    index=tool_index,
                                    call_id=str(getattr(block, "id", "") or ""),
                                    name=str(getattr(block, "name", "") or ""),
                                    arguments=initial_arguments,
                                )
                            ]
                        )
                elif event_type == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    delta_type = getattr(delta, "type", "")
                    if delta_type == "text_delta":
                        text = str(getattr(delta, "text", "") or "")
                        if text:
                            yield _chunk(content=text)
                    elif delta_type == "thinking_delta":
                        thinking = str(getattr(delta, "thinking", "") or "")
                        if thinking:
                            yield _chunk(reasoning=thinking)
                    elif delta_type == "input_json_delta" and index not in full_input_blocks:
                        partial = str(getattr(delta, "partial_json", "") or "")
                        if partial and index in tool_indices:
                            yield _chunk(
                                tool_calls=[
                                    _tool_delta(
                                        index=tool_indices[index],
                                        arguments=partial,
                                    )
                                ]
                            )
                elif event_type == "message_stop":
                    break

    async def list_models(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        async for model in self.client.models.list(limit=100):
            model_id = str(getattr(model, "id", "") or "")
            if model_id:
                result.append(
                    {
                        "id": model_id,
                        "name": str(getattr(model, "display_name", "") or model_id),
                    }
                )
        return result

    async def test_connection(self) -> dict[str, Any]:
        started = time.perf_counter()
        models = await self.list_models()
        return {
            "ok": True,
            "adapter": self.kind,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "models": [item["id"] for item in models],
        }

    async def close(self) -> None:
        await self.client.close()


def build_adapter(
    *,
    provider_key: str,
    provider: dict[str, Any],
    api_key: str,
    model_info: dict[str, Any] | None = None,
) -> OpenAICompatibleAdapter | AnthropicAdapter:
    kind = normalize_adapter(str(provider.get("adapter") or ""), provider_key)
    if kind == "anthropic":
        return AnthropicAdapter(
            provider_key=provider_key,
            provider=provider,
            api_key=api_key,
            model_info=model_info,
        )
    return OpenAICompatibleAdapter(
        provider_key=provider_key,
        provider=provider,
        api_key=api_key,
    )


async def probe_provider(
    *,
    provider_key: str,
    provider: dict[str, Any],
    api_key: str,
    model_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    adapter = build_adapter(
        provider_key=provider_key,
        provider=provider,
        api_key=api_key,
        model_info=model_info,
    )
    try:
        return await adapter.test_connection()
    finally:
        await adapter.close()


def ollama_native_models(base_url: str, api_key: str = "") -> list[dict[str, Any]]:
    """Best-effort Ollama discovery for servers without the OpenAI model list."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    response = httpx.get(f"{root}/api/tags", headers=headers, timeout=10.0, trust_env=False)
    response.raise_for_status()
    payload = response.json()
    result: list[dict[str, Any]] = []
    for item in payload.get("models", []) if isinstance(payload, dict) else []:
        name = str(item.get("name") or item.get("model") or "")
        if name:
            result.append({"id": name, "name": name})
    return result
