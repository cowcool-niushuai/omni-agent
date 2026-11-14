"""Tool contracts: validation, normalized results and retry policy.

The model is an untrusted caller.  Every native tool therefore declares a
Pydantic argument model here; MCP tools are validated against their JSON
Schema by the server that owns them.  The runtime converts validation failures
into a consistent ``[参数错误]`` tool result instead of letting a malformed call
crash the graph.

The helpers also classify tool output so the audit trail can distinguish a
successful command from a policy block, a timeout or a recoverable error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ToolContractError(ValueError):
    """Raised when a model supplied invalid arguments for a native tool."""

    def __init__(self, tool_name: str, detail: str) -> None:
        super().__init__(detail)
        self.tool_name = tool_name
        self.detail = detail


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FileListArgs(_Args):
    directory: str = "."


class FileReadArgs(_Args):
    filepath: str = Field(min_length=1)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1)


class DocumentReadArgs(_Args):
    filepath: str = Field(min_length=1)
    max_chars: int = Field(default=0, ge=0)


class FileWriteArgs(_Args):
    filepath: str = Field(min_length=1)
    content: str


class FileEditArgs(_Args):
    filepath: str = Field(min_length=1)
    target: str = Field(min_length=1)
    replacement: str


class BashRunArgs(_Args):
    command: str = Field(min_length=1)


class PythonEvalArgs(_Args):
    code: str = Field(min_length=1)


class WebFetchArgs(_Args):
    url: str = Field(min_length=1)


class WebSearchArgs(_Args):
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=10)


class BrowserArgs(_Args):
    action: str = Field(min_length=1)
    url: str = ""
    selector: str = ""
    value: str = ""
    key: str = ""
    max_chars: int = Field(default=0, ge=0)
    full_page: bool = False
    filename: str = ""
    milliseconds: int = Field(default=1000, ge=0)


class MemorySaveArgs(_Args):
    category: str = "fact"
    key: str = Field(min_length=1)
    content: str = Field(min_length=1)
    importance: float = Field(default=1.0, ge=0, le=5)


class MemorySearchArgs(_Args):
    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=50)


class KnowledgeAddArgs(_Args):
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    tags: str = ""


class RagSearchArgs(_Args):
    query: str = Field(min_length=1)
    limit: int = Field(default=3, ge=1, le=50)


class ReadSkillArgs(_Args):
    skill_id: str = Field(min_length=1)


class SubagentArgs(_Args):
    role: str = Field(min_length=1)
    task: str = Field(min_length=1)


PlanStatus = Literal["pending", "in_progress", "completed", "blocked"]


class PlanStep(_Args):
    id: str = ""
    title: str = Field(min_length=1)
    status: PlanStatus = "pending"
    acceptance: str = ""
    evidence: str = ""


class UpdatePlanArgs(_Args):
    goal: str = Field(min_length=1)
    done_when: str = ""
    steps: list[PlanStep] = Field(default_factory=list, max_length=20)


NATIVE_MODELS: dict[str, type[BaseModel]] = {
    "file_list": FileListArgs,
    "file_read": FileReadArgs,
    "document_read": DocumentReadArgs,
    "file_write": FileWriteArgs,
    "file_edit": FileEditArgs,
    "bash_run": BashRunArgs,
    "python_eval": PythonEvalArgs,
    "web_fetch": WebFetchArgs,
    "web_search": WebSearchArgs,
    "browser": BrowserArgs,
    "memory_save": MemorySaveArgs,
    "memory_search": MemorySearchArgs,
    "knowledge_add": KnowledgeAddArgs,
    "rag_search": RagSearchArgs,
    "read_skill_doc": ReadSkillArgs,
    "spawn_subagent": SubagentArgs,
    "update_plan": UpdatePlanArgs,
}

# Calls that are safe to repeat when the first attempt raised an exception.
IDEMPOTENT_TOOLS = {
    "file_list",
    "file_read",
    "document_read",
    "memory_search",
    "rag_search",
    "read_skill_doc",
    "web_fetch",
}

# Read-only browser actions can be retried; click/fill/press cannot.
IDEMPOTENT_BROWSER_ACTIONS = {
    "goto",
    "text",
    "status",
    "back",
    "forward",
    "reload",
    "wait",
    "screenshot",
}


def is_idempotent(tool_name: str, arguments: dict[str, Any]) -> bool:
    if tool_name == "browser":
        return str(arguments.get("action", "")) in IDEMPOTENT_BROWSER_ACTIONS
    return tool_name in IDEMPOTENT_TOOLS


def _format_error(error: ValidationError) -> str:
    parts: list[str] = []
    for item in error.errors()[:4]:
        location = ".".join(str(value) for value in item.get("loc", ()))
        message = str(item.get("msg", "invalid value"))
        parts.append(f"{location or 'arguments'}: {message}")
    return "；".join(parts) or "参数不合法"


def validate_tool_call(
    tool_name: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """Return ``(normalized_arguments, error_message)``.

    Native tools must match their contract exactly.  MCP tools keep the
    arguments supplied by the model; their server validates them with the
    advertised JSON Schema and returns an MCP error if they are wrong.
    """
    model = NATIVE_MODELS.get(tool_name)
    if model is None:
        return dict(arguments or {}), None
    try:
        parsed = model.model_validate(arguments or {})
    except ValidationError as exc:
        detail = _format_error(exc)
        raise ToolContractError(tool_name, detail) from exc
    return parsed.model_dump(), None


@dataclass(frozen=True)
class ToolOutcome:
    ok: bool
    error_code: str | None = None
    retryable: bool = False
    message: str = ""


_FAILURE_MARKERS = (
    "失败",
    "错误",
    "不存在",
    "无效",
    "不支持",
    "超时",
    "不能为空",
)
_BLOCK_MARKERS = (
    "[安全策略拒绝]",
    "[严重越权拒绝]",
    "[用户拒绝]",
    "[自动拒绝]",
    "[子智能体策略拒绝]",
)


def classify_output(tool_name: str, output: str) -> ToolOutcome:
    """Classify a tool's textual output for status, audit and UI purposes."""
    text = str(output or "")
    stripped = text.strip()
    if not stripped:
        return ToolOutcome(ok=False, error_code="EMPTY", message=text)
    if stripped.startswith("[参数错误]"):
        return ToolOutcome(ok=False, error_code="INVALID_ARGUMENTS", message=text)
    if stripped.startswith("[工具超时]"):
        return ToolOutcome(ok=False, error_code="TIMEOUT", retryable=True, message=text)
    if any(stripped.startswith(marker) for marker in _BLOCK_MARKERS):
        return ToolOutcome(ok=False, error_code="BLOCKED", message=text)
    if stripped.startswith("未知工具:"):
        return ToolOutcome(ok=False, error_code="UNKNOWN_TOOL", message=text)
    if any(marker in stripped[:160] for marker in _FAILURE_MARKERS):
        # A "not found" result is recoverable but still not a successful call.
        return ToolOutcome(ok=False, error_code="TOOL_ERROR", message=text)
    return ToolOutcome(ok=True, message=text)


def error_result(tool_name: str, detail: str) -> str:
    return f"[参数错误] 工具 {tool_name} 参数校验失败：{detail}"


def plan_is_satisfied(steps: list[dict[str, Any]]) -> tuple[bool, str]:
    """Return whether a plan is complete enough to finish the turn."""
    if not steps:
        return False, "计划为空，至少需要一个步骤。"
    open_steps = [
        step
        for step in steps
        if str(step.get("status") or "pending") in {"pending", "in_progress"}
    ]
    if open_steps:
        titles = "、".join(str(step.get("title")) for step in open_steps[:3])
        return False, f"仍有未完成步骤：{titles}。请继续执行并更新计划状态。"
    no_evidence = [
        step
        for step in steps
        if str(step.get("status") or "") == "completed"
        and not str(step.get("evidence") or "").strip()
    ]
    if no_evidence:
        titles = "、".join(str(step.get("title")) for step in no_evidence[:3])
        return False, f"已完成步骤缺少验证证据：{titles}。请执行验证并填写 evidence。"
    return True, ""
