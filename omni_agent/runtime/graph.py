"""LangGraph-based agent runtime.

The graph is deliberately explicit (no ``create_agent`` black box):

    START -> agent -> (approval?) -> tools -> agent -> ... -> END

* ``agent`` streams real model tokens and emits custom events.
* ``approval`` uses ``interrupt()`` so a human decision is durable and
  resumable via ``Command(resume=...)``.
* ``tools`` executes independent calls concurrently and applies the
  permission policy before every call.
"""

from __future__ import annotations

import asyncio
import json
import operator
import re
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, AsyncGenerator, Callable, TypedDict

import structlog
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

import audit
import database
import permission
import prompts
import tools
from model_manager import model_manager
from omni_agent.config import get_settings
from omni_agent.runtime.context import maybe_compact
from omni_agent.security.limits import truncate_text
from omni_agent.security.sandbox import detect_backend

log = structlog.get_logger("omni.runtime")


class AgentState(TypedDict, total=False):
    session_id: str
    messages: Annotated[list[dict[str, Any]], operator.add]
    summary: str | None
    summary_upto: int
    pending_tool_calls: list[dict[str, Any]]
    approval_action: str | None
    iteration: int
    max_iterations: int
    thoughts: Annotated[list[str], operator.add]
    tool_calls_record: Annotated[list[dict[str, Any]], operator.add]
    final_answer: str | None
    error: str | None
    turn_id: str
    turn_start_thoughts: int
    turn_start_records: int
    last_saved_turn: str | None


ClientFactory = Callable[[], tuple[Any, str]]


def _writer() -> Callable[[dict[str, Any]], None] | None:
    try:
        return get_stream_writer()
    except Exception:  # pragma: no cover - only outside a stream
        return None


def _emit(writer: Callable[[dict[str, Any]], None] | None, event: dict[str, Any]) -> None:
    if writer is not None:
        writer(event)


def _message_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


class AgentRuntime:
    def __init__(
        self,
        *,
        checkpoint_path: Path | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        settings = get_settings()
        self._settings = settings
        self._checkpoint_path = checkpoint_path or (
            Path(settings.data_dir) / "checkpoints.db"
        )
        self._client_factory: ClientFactory = (
            client_factory or model_manager.get_async_client_and_model
        )
        self._checkpointer_cm: Any = None
        self._checkpointer: Any = None
        self._graph: Any = None
        self._lock = asyncio.Lock()
        self._active_sessions: set[str] = set()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        if self._graph is not None:
            return
        async with self._lock:
            if self._graph is not None:
                return
            self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            self._checkpointer_cm = AsyncSqliteSaver.from_conn_string(
                str(self._checkpoint_path)
            )
            self._checkpointer = await self._checkpointer_cm.__aenter__()
            self._graph = self._build_graph()

    async def close(self) -> None:
        async with self._lock:
            if self._checkpointer_cm is not None:
                await self._checkpointer_cm.__aexit__(None, None, None)
            self._checkpointer_cm = None
            self._checkpointer = None
            self._graph = None

    async def _get_graph(self) -> Any:
        if self._graph is None:
            await self.initialize()
        return self._graph

    def _build_graph(self) -> Any:
        builder = StateGraph(AgentState)
        builder.add_node("agent", self._agent_node)
        builder.add_node("approval", self._approval_node)
        builder.add_node("tools", self._tools_node)
        builder.add_edge(START, "agent")
        builder.add_conditional_edges(
            "agent",
            self._route_after_agent,
            {"approval": "approval", "tools": "tools", "end": END},
        )
        builder.add_edge("approval", "tools")
        builder.add_edge("tools", "agent")
        return builder.compile(checkpointer=self._checkpointer)

    # ------------------------------------------------------------------
    # graph nodes
    # ------------------------------------------------------------------
    async def _agent_node(self, state: AgentState) -> dict[str, Any]:
        writer = _writer()
        iteration = int(state.get("iteration", 0)) + 1
        max_iterations = int(state.get("max_iterations") or self._settings.max_iterations)
        session_id = str(state.get("session_id", ""))

        if iteration > max_iterations:
            text = (
                f"智能体多轮推理已达上限（{max_iterations} 轮），"
                "已保存当前执行过程与中间文件。"
            )
            _emit(writer, {"type": "answer_chunk", "content": text})
            return {
                "messages": [{"role": "assistant", "content": text}],
                "iteration": iteration,
                "pending_tool_calls": [],
                "final_answer": text,
            }

        client, model_name = self._client_factory()
        messages = list(state.get("messages") or [])
        summary, summary_upto, dropped = await maybe_compact(
            messages=messages,
            summary=state.get("summary"),
            summary_upto=int(state.get("summary_upto", 0)),
            client=client,
            model=model_name,
            max_tokens=int(self._settings.context_max_tokens),
            keep_last=int(self._settings.context_keep_last),
        )
        if dropped:
            _emit(
                writer,
                {
                    "type": "compaction",
                    "dropped": dropped,
                    "summary_chars": len(summary or ""),
                },
            )

        model_messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompts.build_system_prompt()}
        ]
        if summary:
            model_messages.append(
                {
                    "role": "system",
                    "content": "以下是较早对话的压缩摘要，请结合它继续工作：\n" + summary,
                }
            )
        model_messages.extend(messages[summary_upto:])

        tool_schemas = tools.get_all_tool_definitions()
        try:
            content, reasoning, tool_calls, parsed_arguments = await self._stream_completion(
                client=client,
                model=model_name,
                messages=model_messages,
                tool_schemas=tool_schemas,
                writer=writer,
            )
        except Exception as exc:
            log.exception("agent_model_call_failed", session_id=session_id)
            text = f"模型调用失败：{exc}"
            _emit(writer, {"type": "error", "message": text})
            return {
                "messages": [{"role": "assistant", "content": text}],
                "iteration": iteration,
                "pending_tool_calls": [],
                "final_answer": text,
                "error": str(exc),
            }

        if reasoning:
            _emit(writer, {"type": "thought", "content": reasoning})

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": content,
        }
        updates: dict[str, Any] = {
            "iteration": iteration,
            "summary": summary,
            "summary_upto": summary_upto,
            "thoughts": [reasoning] if reasoning else [],
        }

        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
            pending: list[dict[str, Any]] = []
            for call, args in zip(tool_calls, parsed_arguments, strict=False):
                pending.append(
                    {
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "arguments": args,
                    }
                )
            updates["messages"] = [assistant_message]
            updates["pending_tool_calls"] = pending
            updates["final_answer"] = None
            if content:
                # Content was streamed optimistically; it is preamble before
                # a tool call, so move it into the thought trace.
                _emit(writer, {"type": "answer_discard"})
                _emit(writer, {"type": "thought", "content": content})
                updates["thoughts"] = list(updates["thoughts"]) + [content]
            for call in pending:
                _emit(
                    writer,
                    {
                        "type": "tool_start",
                        "tool_call_id": call["id"],
                        "name": call["name"],
                        "arguments": call["arguments"],
                    },
                )
            return updates

        updates["messages"] = [assistant_message]
        updates["pending_tool_calls"] = []
        updates["final_answer"] = content
        return updates

    def _route_after_agent(self, state: AgentState) -> str:
        calls = state.get("pending_tool_calls") or []
        if not calls:
            return "end"
        for call in calls:
            risk, _ = permission.classify_permission(
                str(call.get("name", "")), call.get("arguments") or {}
            )
            if risk == permission.PermissionRisk.ASK_USER:
                return "approval"
        return "tools"

    async def _approval_node(self, state: AgentState) -> dict[str, Any]:
        session_id = str(state.get("session_id", ""))
        calls = state.get("pending_tool_calls") or []
        ask_requests: list[dict[str, Any]] = []

        for call in calls:
            name = str(call.get("name", ""))
            arguments = call.get("arguments") or {}
            risk, reason = permission.classify_permission(name, arguments)
            if risk != permission.PermissionRisk.ASK_USER:
                continue
            request_id = permission.permission_request_id(
                session_id, str(call.get("id", ""))
            )
            permission.create_permission_request(
                request_id, session_id, name, arguments, risk, reason
            )
            ask_requests.append(
                {
                    "request_id": request_id,
                    "tool_call_id": call.get("id"),
                    "tool_name": name,
                    "arguments": arguments,
                    "reason": reason,
                }
            )

        if not ask_requests:
            return {"approval_action": "APPROVE"}

        batch_id = permission.permission_batch_id(
            session_id, [str(item["tool_call_id"]) for item in ask_requests]
        )
        payload = {
            "type": "permission_batch",
            "batch_id": batch_id,
            "session_id": session_id,
            "requests": ask_requests,
            "reason": f"本次操作包含 {len(ask_requests)} 个需要人工审批的高危调用。",
        }

        decision = interrupt(payload)
        if not isinstance(decision, dict):
            decision = {}
        action = str(decision.get("action", "REJECT")).upper()
        if decision.get("batch_id") and decision.get("batch_id") != batch_id:
            action = "REJECT"
        request_ids = [str(item["request_id"]) for item in ask_requests]
        if action == "APPROVE":
            permission.approve_requests(request_ids)
        else:
            action = "REJECT"
            permission.reject_requests(request_ids)
        return {"approval_action": action}

    async def _tools_node(self, state: AgentState) -> dict[str, Any]:
        session_id = str(state.get("session_id", ""))
        turn_id = str(state.get("turn_id", ""))
        action = str(state.get("approval_action") or "REJECT")
        calls = state.get("pending_tool_calls") or []
        writer = _writer()
        settings = self._settings
        semaphore = asyncio.Semaphore(max(1, settings.tool_max_concurrency))
        backend = detect_backend(settings.sandbox_backend)

        async def run_one(call: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            async with semaphore:
                name = str(call.get("name", ""))
                arguments = call.get("arguments") or {}
                tool_call_id = str(call.get("id", ""))
                risk, reason = permission.classify_permission(name, arguments)
                started = time.perf_counter()
                output = ""
                status = "OK"
                error: str | None = None

                if risk == permission.PermissionRisk.DENY:
                    output = f"[严重越权拒绝] 系统已阻断该操作：{reason}"
                    status = "BLOCKED"
                    decision = "DENIED"
                elif (
                    risk == permission.PermissionRisk.ASK_USER
                    and action != "APPROVE"
                ):
                    output = f"[用户拒绝] 该高危操作未获批准：{reason}"
                    status = "BLOCKED"
                    decision = "REJECTED"
                else:
                    decision = (
                        "APPROVED"
                        if risk == permission.PermissionRisk.ASK_USER
                        else "AUTO"
                    )
                    timeout = (
                        settings.subagent_timeout_seconds
                        if name == "spawn_subagent"
                        else settings.tool_timeout_seconds
                    )
                    try:
                        output = await asyncio.wait_for(
                            self._execute_tool(
                                session_id=session_id,
                                name=name,
                                arguments=arguments,
                                tool_call_id=tool_call_id,
                                writer=writer,
                            ),
                            timeout=timeout,
                        )
                        if output.startswith("工具执行发生异常"):
                            status = "ERROR"
                            error = output[:500]
                    except TimeoutError:
                        output = (
                            f"[工具超时] {name} 执行超过 {timeout:g} 秒，"
                            "已放弃等待。后台线程可能仍在收尾。"
                        )
                        status = "TIMEOUT"
                        error = output

                output = truncate_text(output, settings.tool_max_output_chars)
                duration_ms = int((time.perf_counter() - started) * 1000)
                exit_code_match = re.search(r"\[Exit code:\s*(-?\d+)\]", output)
                exit_code = int(exit_code_match.group(1)) if exit_code_match else None
                sandbox_name = backend if name in {"bash_run", "python_eval"} else None

                if settings.audit_enabled:
                    audit.record_tool_call(
                        session_id=session_id,
                        turn_id=turn_id or None,
                        tool_call_id=tool_call_id,
                        tool_name=name,
                        arguments=arguments,
                        risk_level=risk,
                        decision=decision,
                        status=status,
                        sandbox=sandbox_name,
                        exit_code=exit_code,
                        duration_ms=duration_ms,
                        output_chars=len(output),
                        output_preview=output,
                        error=error,
                    )

                _emit(
                    writer,
                    {
                        "type": "tool_result",
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "result": output,
                    },
                )
                record = {
                    "id": tool_call_id,
                    "name": name,
                    "arguments": arguments,
                    "output": output[:1000],
                }
                tool_message = {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": str(output),
                }
                return tool_message, record

        results = await asyncio.gather(*(run_one(call) for call in calls))
        messages = [item[0] for item in results]
        records = [item[1] for item in results]
        return {
            "messages": messages,
            "pending_tool_calls": [],
            "approval_action": None,
            "tool_calls_record": records,
        }

    # ------------------------------------------------------------------
    # model + tools
    # ------------------------------------------------------------------
    async def _stream_completion(
        self,
        *,
        client: Any,
        model: str,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        writer: Callable[[dict[str, Any]], None] | None,
    ) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "stream": True,
        }
        if tool_schemas:
            kwargs["tools"] = tool_schemas
            kwargs["tool_choice"] = "auto"

        stream = await client.chat.completions.create(**kwargs)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_parts: dict[int, dict[str, Any]] = {}

        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = choices[0].delta
            extra = getattr(delta, "model_extra", None) or {}
            reasoning = getattr(delta, "reasoning_content", None) or extra.get(
                "reasoning_content"
            )
            if reasoning:
                reasoning_parts.append(str(reasoning))
            text = getattr(delta, "content", None)
            if text:
                content_parts.append(str(text))
                _emit(writer, {"type": "answer_chunk", "content": str(text)})

            for call_delta in getattr(delta, "tool_calls", None) or []:
                index = int(getattr(call_delta, "index", 0) or 0)
                part = tool_parts.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if getattr(call_delta, "id", None):
                    part["id"] = str(call_delta.id)
                function = getattr(call_delta, "function", None)
                if function is not None:
                    if getattr(function, "name", None):
                        part["function"]["name"] += str(function.name)
                    if getattr(function, "arguments", None):
                        part["function"]["arguments"] += str(function.arguments)

        tool_calls: list[dict[str, Any]] = []
        parsed_arguments: list[dict[str, Any]] = []
        for index in sorted(tool_parts):
            part = tool_parts[index]
            if not part["function"]["name"]:
                continue
            if not part["id"]:
                part["id"] = f"call_{uuid.uuid4().hex[:12]}"
            raw_arguments = part["function"]["arguments"] or "{}"
            try:
                parsed = json.loads(raw_arguments)
            except json.JSONDecodeError:
                parsed = {"raw": raw_arguments}
            if not isinstance(parsed, dict):
                parsed = {"value": parsed}
            tool_calls.append(part)
            parsed_arguments.append(parsed)

        return (
            "".join(content_parts),
            "".join(reasoning_parts),
            tool_calls,
            parsed_arguments,
        )

    async def _execute_tool(
        self,
        *,
        session_id: str,
        name: str,
        arguments: dict[str, Any],
        tool_call_id: str,
        writer: Callable[[dict[str, Any]], None] | None,
    ) -> str:
        try:
            if name == "spawn_subagent":
                role = str(arguments.get("role", "专属专家子智能体"))
                task = str(arguments.get("task", ""))
                _emit(
                    writer,
                    {
                        "type": "subagent_start",
                        "role": role,
                        "task": task,
                        "tool_call_id": tool_call_id,
                    },
                )
                output = await self.run_subagent_task(session_id, role, task)
                _emit(
                    writer,
                    {
                        "type": "subagent_done",
                        "role": role,
                        "result": output,
                        "tool_call_id": tool_call_id,
                    },
                )
                return output
            return await asyncio.to_thread(tools.dispatch_tool, name, arguments)
        except Exception as exc:
            log.exception("tool_execution_failed", tool=name)
            return f"工具执行发生异常: {exc}"

    async def run_subagent_task(self, session_id: str, role: str, task: str) -> str:
        """Run an isolated sub-agent with the same policy, without recursion."""
        sub_id = "sub_" + uuid.uuid4().hex[:8]
        database.record_subagent_task(sub_id, session_id, role, task)

        sub_system = f"""你是专职于【{role}】的专属子智能体 (Sub-Agent)。
受主智能体指派，你负责高效、专注地完成以下独立子任务：
【子任务目标】：{task}

工作区位于 ./workspace。
你可以使用基础工具（file_write, file_read, file_list, bash_run, python_eval,
web_fetch, rag_search）来执行调查、编写代码或数据分析。
【行动要求】：
1. 专注完成本子任务，不要发散。
2. 如需写代码或生成文件，请直接在 ./workspace 中产出。
3. 任务完成后，提供精炼、清晰的结构化总结汇报。
"""
        subagent_tools = [
            item
            for item in tools.get_all_tool_definitions()
            if item["function"]["name"] != "spawn_subagent"
        ]
        sub_messages: list[dict[str, Any]] = [
            {"role": "system", "content": sub_system},
            {"role": "user", "content": f"请开始执行子任务: {task}"},
        ]

        final_report = ""
        try:
            client, model_name = self._client_factory()
            for _ in range(4):
                kwargs: dict[str, Any] = {
                    "model": model_name,
                    "messages": sub_messages,
                    "temperature": 0.2,
                }
                if subagent_tools:
                    kwargs["tools"] = subagent_tools
                    kwargs["tool_choice"] = "auto"
                response = await client.chat.completions.create(**kwargs)
                message = response.choices[0].message
                if not message.tool_calls:
                    final_report = message.content or "子任务已执行完成。"
                    break

                assistant = message.model_dump(exclude_none=True)
                assistant.pop("reasoning_content", None)
                sub_messages.append(assistant)
                for call in message.tool_calls:
                    name = call.function.name
                    try:
                        arguments = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    risk, reason = permission.classify_permission(name, arguments)
                    if risk in (
                        permission.PermissionRisk.ASK_USER,
                        permission.PermissionRisk.DENY,
                    ):
                        output = f"[子智能体策略拒绝] {reason}"
                    else:
                        output = await asyncio.to_thread(
                            tools.dispatch_tool, name, arguments
                        )
                    sub_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": str(output),
                        }
                    )
        except Exception as exc:
            log.exception("subagent_failed", role=role)
            final_report = f"子智能体执行失败: {exc}"

        if not final_report:
            final_report = f"子智能体【{role}】已执行相关操作并产出中间结果。"
        database.update_subagent_task(sub_id, "COMPLETED", final_report)
        return f"【子智能体 ({role}) 汇报】\n{final_report}"

    # ------------------------------------------------------------------
    # public streaming API
    # ------------------------------------------------------------------
    async def run_react_stream(
        self,
        session_id: str,
        user_message: str,
        max_iterations: int = 5,
    ) -> AsyncGenerator[dict[str, Any], None]:
        async for event in self.run_stream(
            session_id, user_message, max_iterations=max_iterations
        ):
            yield event

    async def run_stream(
        self,
        session_id: str,
        user_message: str,
        *,
        max_iterations: int | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        if session_id in self._active_sessions:
            yield {
                "type": "error",
                "message": "当前会话已有任务在运行，请等待完成或先处理审批请求。",
            }
            return
        self._active_sessions.add(session_id)
        try:
            async for event in self._run_stream_inner(
                session_id, user_message, max_iterations=max_iterations
            ):
                yield event
        finally:
            self._active_sessions.discard(session_id)

    async def _run_stream_inner(
        self,
        session_id: str,
        user_message: str,
        *,
        max_iterations: int | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        database.ensure_session(session_id)
        database.save_message(session_id, "user", user_message)

        graph = await self._get_graph()
        config = {"configurable": {"thread_id": session_id}}
        state = await graph.aget_state(config)
        if state.interrupts:
            yield {
                "type": "error",
                "message": "当前会话还有未处理的审批请求，请先允许或拒绝后再发送新消息。",
            }
            return

        values = state.values or {}
        turn_id = uuid.uuid4().hex
        if values.get("messages"):
            input_state: dict[str, Any] = {
                "messages": [{"role": "user", "content": user_message}],
            }
            turn_start_thoughts = len(values.get("thoughts") or [])
            turn_start_records = len(values.get("tool_calls_record") or [])
        else:
            history = database.get_session_messages(session_id)
            seed: list[dict[str, Any]] = []
            for message in history:
                role = str(message.get("role", ""))
                content = str(message.get("content") or "")
                if role in {"user", "assistant"} and content:
                    seed.append({"role": role, "content": content})
            if not seed:
                seed = [{"role": "user", "content": user_message}]
            input_state = {"messages": seed}
            turn_start_thoughts = 0
            turn_start_records = 0

        input_state.update(
            {
                "session_id": session_id,
                "iteration": 0,
                "max_iterations": int(
                    max_iterations or self._settings.max_iterations
                ),
                "summary": values.get("summary") if values.get("messages") else None,
                "summary_upto": (
                    int(values.get("summary_upto", 0))
                    if values.get("messages")
                    else 0
                ),
                "pending_tool_calls": [],
                "approval_action": None,
                "final_answer": None,
                "error": None,
                "turn_id": turn_id,
                "turn_start_thoughts": turn_start_thoughts,
                "turn_start_records": turn_start_records,
            }
        )

        try:
            async for event in self._stream_graph(graph, input_state, config):
                yield event
        except Exception as exc:
            log.exception("agent_run_failed", session_id=session_id)
            yield {"type": "error", "message": f"Agent 运行失败: {exc}"}
            return

        done = await self._finish_turn(graph, config, session_id, turn_id)
        if done is not None:
            yield done

    async def resume_stream(
        self,
        session_id: str,
        request_id: str,
        action: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        if session_id in self._active_sessions:
            yield {
                "type": "error",
                "message": "当前会话已有任务在运行，请等待完成后再处理审批。",
            }
            return
        self._active_sessions.add(session_id)
        try:
            async for event in self._resume_stream_inner(
                session_id, request_id, action
            ):
                yield event
        finally:
            self._active_sessions.discard(session_id)

    async def _resume_stream_inner(
        self,
        session_id: str,
        request_id: str,
        action: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        graph = await self._get_graph()
        config = {"configurable": {"thread_id": session_id}}
        state = await graph.aget_state(config)
        if not state.interrupts:
            yield {
                "type": "error",
                "message": "没有找到待审批的操作，可能已经处理过了。",
            }
            return

        payload = self._interrupt_payload(state.interrupts)
        expected = str(payload.get("batch_id", ""))
        if expected and request_id and expected != request_id:
            yield {"type": "error", "message": "审批请求已失效，请重新发起任务。"}
            return

        values = state.values or {}
        turn_id = str(values.get("turn_id") or uuid.uuid4().hex)
        command: Any = Command(resume={"action": action, "batch_id": expected or request_id})
        try:
            async for event in self._stream_graph(graph, command, config):
                yield event
        except Exception as exc:
            log.exception("agent_resume_failed", session_id=session_id)
            yield {"type": "error", "message": f"Agent 恢复失败: {exc}"}
            return

        done = await self._finish_turn(graph, config, session_id, turn_id)
        if done is not None:
            yield done

    async def _stream_graph(
        self,
        graph: Any,
        graph_input: Any,
        config: dict[str, Any],
    ) -> AsyncGenerator[dict[str, Any], None]:
        emitted_interrupt = False
        async for part in graph.astream(
            graph_input,
            config,
            stream_mode=["custom", "updates"],
            version="v2",
        ):
            if part.get("type") == "custom":
                event = part.get("data")
                if isinstance(event, dict):
                    yield event
            elif part.get("type") == "updates":
                data = part.get("data") or {}
                if "__interrupt__" in data:
                    interrupts = data["__interrupt__"]
                    payload = self._interrupt_payload(interrupts)
                    emitted_interrupt = True
                    yield self._permission_event(payload)

        if not emitted_interrupt:
            state = await graph.aget_state(config)
            if state.interrupts:
                payload = self._interrupt_payload(state.interrupts)
                yield self._permission_event(payload)

    def _interrupt_payload(self, interrupts: Any) -> dict[str, Any]:
        if not interrupts:
            return {}
        first = interrupts[0]
        value = getattr(first, "value", first)
        return value if isinstance(value, dict) else {"value": value}

    def _permission_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("type") == "permission_batch":
            requests = payload.get("requests") or []
            return {
                "type": "permission_intercept",
                "request_id": payload.get("batch_id"),
                "session_id": payload.get("session_id"),
                "tool_name": "permission_batch",
                "arguments": {"requests": requests},
                "reason": payload.get("reason"),
                "requests": requests,
            }
        return {"type": "permission_intercept", **payload}

    async def _finish_turn(
        self,
        graph: Any,
        config: dict[str, Any],
        session_id: str,
        turn_id: str,
    ) -> dict[str, Any] | None:
        state = await graph.aget_state(config)
        if state.interrupts:
            return None

        values = state.values or {}
        final_answer = values.get("final_answer")
        if final_answer is None:
            return None

        if str(values.get("last_saved_turn") or "") == turn_id:
            return {
                "type": "done",
                "total_steps": int(values.get("iteration", 0)),
            }

        thoughts = list(values.get("thoughts") or [])
        thought_start = int(values.get("turn_start_thoughts", 0))
        turn_thoughts = thoughts[thought_start:]

        records = list(values.get("tool_calls_record") or [])
        record_start = int(values.get("turn_start_records", 0))
        turn_records = records[record_start:]

        database.save_message(
            session_id=session_id,
            role="assistant",
            content=str(final_answer),
            thought="\n\n".join(turn_thoughts) if turn_thoughts else None,
            tool_calls=turn_records if turn_records else None,
        )
        await graph.aupdate_state(config, {"last_saved_turn": turn_id})
        return {
            "type": "done",
            "total_steps": int(values.get("iteration", 0)),
        }


runtime = AgentRuntime()
