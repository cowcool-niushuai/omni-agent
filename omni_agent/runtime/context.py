"""Token accounting and context compaction for the agent runtime."""

from __future__ import annotations

import json
from typing import Any

import structlog

log = structlog.get_logger("omni.context")

try:
    import tiktoken

    _ENCODER: Any = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover - offline fallback
    _ENCODER = None


def _text_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENCODER is not None:
        return len(_ENCODER.encode(text, disallowed_special=()))
    # Conservative approximation for environments without tiktoken data.
    return max(1, len(text) // 3)


def message_tokens(message: dict[str, Any]) -> int:
    total = 4  # message framing overhead
    total += _text_tokens(str(message.get("content") or ""))
    tool_calls = message.get("tool_calls")
    if tool_calls:
        total += _text_tokens(json.dumps(tool_calls, ensure_ascii=False))
    return total


def count_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(message_tokens(message) for message in messages)


def _render_for_summary(message: dict[str, Any], limit: int = 1200) -> str:
    role = message.get("role", "unknown")
    content = str(message.get("content") or "")
    if len(content) > limit:
        content = content[:limit] + "…"
    line = f"[{role}] {content}"
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        names = []
        for call in tool_calls:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            names.append(str(function.get("name", "?")))
        line += f" (tool_calls: {', '.join(names)})"
    return line


def _find_cut(messages: list[dict[str, Any]], keep_last: int, floor: int) -> int:
    cut = max(floor, len(messages) - keep_last)
    # Never start the retained window with a tool result: its assistant
    # tool-call message would be in the summary instead.
    while cut < len(messages) and messages[cut].get("role") == "tool":
        cut += 1
    return cut


def _local_summary(messages: list[dict[str, Any]]) -> str:
    tool_names: list[str] = []
    user_goals: list[str] = []
    for message in messages:
        if message.get("role") == "user":
            content = str(message.get("content") or "").strip()
            if content:
                user_goals.append(content[:200])
        for call in message.get("tool_calls") or []:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            name = str(function.get("name", ""))
            if name and name not in tool_names:
                tool_names.append(name)
    tools = f"；涉及工具：{', '.join(tool_names)}" if tool_names else ""
    goals = "；".join(user_goals[-3:]) or "（未记录）"
    return (
        f"已压缩 {len(messages)} 条较早的对话消息{tools}。\n"
        f"- 用户目标：{goals}\n"
        "- 关键决策：见上方对话与工具结果\n"
        "- 修改文件：见工具输出中的文件路径\n"
        "- 错误与未完成事项：需在后续步骤中继续确认"
    )


async def maybe_compact(
    *,
    messages: list[dict[str, Any]],
    summary: str | None,
    summary_upto: int,
    client: Any,
    model: str,
    max_tokens: int = 24_000,
    keep_last: int = 12,
) -> tuple[str | None, int, int]:
    """Compact old messages when the context grows too large.

    Returns ``(summary, summary_upto, dropped)``. ``messages[summary_upto:]``
    is the part that still has to be sent to the model.
    """
    if summary_upto > len(messages):
        summary_upto = len(messages)

    active = messages[summary_upto:]
    summary_tokens = _text_tokens(summary or "")
    if summary_tokens + count_tokens(active) <= max_tokens:
        return summary, summary_upto, 0

    cut = _find_cut(messages, keep_last, summary_upto)
    dropped = cut - summary_upto
    if dropped <= 0:
        return summary, summary_upto, 0

    to_summarize = messages[summary_upto:cut]
    rendered = "\n".join(_render_for_summary(message) for message in to_summarize)
    previous = f"已有摘要：\n{summary}\n\n" if summary else ""
    prompt = (
        "你是一个上下文压缩器。请把下面的 Agent 对话压缩成一份事实性、结构化摘要，"
        "供后续继续执行任务时使用。严格按以下小节输出，禁止寒暄和编造：\n"
        "## 用户目标\n## 已确认决策\n## 修改/创建的文件\n"
        "## 关键工具结果与证据\n## 错误与风险\n## 未完成事项\n## 下一步\n"
        "控制在 900 字以内；不确定的信息标注“未确认”。\n\n"
        f"{previous}待压缩对话：\n{rendered}"
    )

    new_summary: str | None = None
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是严谨的上下文压缩器，只输出摘要。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=900,
            stream=False,
        )
        new_summary = (response.choices[0].message.content or "").strip()
    except Exception as exc:  # pragma: no cover - network dependent
        log.warning("context_compaction_failed", error=str(exc), dropped=dropped)

    if not new_summary:
        new_summary = _local_summary(to_summarize)

    return new_summary, cut, dropped
