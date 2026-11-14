"""Unattended scheduled tasks for OmniAgent.

Scheduled runs reuse the normal LangGraph runtime, but use ``auto_reject``
approval mode: any ``ASK_USER`` tool call is rejected instead of pausing for a
human. The model receives the rejection, can adapt, and the whole run is
recorded in the normal message history and audit trail.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import aclosing, suppress
from typing import Any

import structlog

import database
from omni_agent import tenancy
from omni_agent.config import get_settings
from omni_agent.runtime.graph import runtime as default_runtime
from omni_agent.security.limits import truncate_text

log = structlog.get_logger("omni.scheduler")


class Scheduler:
    """Small, single-worker scheduler with database-backed due dates."""

    def __init__(
        self,
        runtime: Any | None = None,
        *,
        poll_seconds: float | None = None,
    ) -> None:
        self._settings = get_settings()
        self._runtime = runtime or default_runtime
        self._poll_seconds = float(
            poll_seconds
            if poll_seconds is not None
            else self._settings.scheduler_poll_seconds
        )
        self._loop_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._running: set[str] = set()
        self._queued: set[str] = set()
        self._background: set[asyncio.Task[Any]] = set()

    @property
    def running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def active_task_id(self) -> str | None:
        return next(iter(self._running), None)

    async def start(self) -> None:
        recovered = database.recover_scheduled_tasks()
        if recovered:
            log.warning("scheduled_tasks_recovered", count=recovered)
        if not self._settings.scheduler_enabled:
            log.info("scheduler_disabled")
            return
        if self.running:
            return
        self._stopping.clear()
        self._loop_task = asyncio.create_task(self._run_loop(), name="omni-scheduler")
        log.info(
            "scheduler_started",
            poll_seconds=self._poll_seconds,
            timeout_seconds=self._settings.scheduler_task_timeout_seconds,
        )

    async def stop(self) -> None:
        self._stopping.set()
        task = self._loop_task
        self._loop_task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        for background in list(self._background):
            background.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
            self._background.clear()
        log.info("scheduler_stopped")

    async def _run_loop(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    await self.run_due()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("scheduler_poll_failed")
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self._poll_seconds
                    )
        finally:
            log.info("scheduler_loop_exit")

    async def run_due(self) -> list[dict[str, Any]]:
        due = database.due_scheduled_tasks(
            limit=self._settings.scheduler_max_due_per_poll
        )
        results: list[dict[str, Any]] = []
        for task in due:
            if self._stopping.is_set():
                break
            results.append(await self.execute(str(task["id"])))
        return results

    def trigger(self, task_id: str) -> bool:
        """Queue a manual run in the background; return False if already busy."""
        if self._running or self._queued:
            return False
        if not database.get_scheduled_task(task_id):
            return False
        self._queued.add(task_id)
        background = asyncio.create_task(
            self._run_triggered(task_id), name=f"omni-schedule-{task_id}"
        )
        self._background.add(background)
        background.add_done_callback(self._background.discard)
        return True

    async def _run_triggered(self, task_id: str) -> None:
        self._queued.discard(task_id)
        try:
            await self.execute(task_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduled_manual_run_failed", task_id=task_id)

    async def execute(self, task_id: str) -> dict[str, Any]:
        """Run one scheduled task now and persist its outcome."""
        if task_id in self._running:
            return {"started": False, "task_id": task_id, "reason": "already_running"}
        task = database.get_scheduled_task(task_id, include_all=True)
        if not task:
            return {"started": False, "task_id": task_id, "reason": "not_found"}

        project_id = str(task.get("project_id") or tenancy.DEFAULT_PROJECT_ID)
        context = tenancy.TenantContext(
            user_id=str(task.get("owner_id") or tenancy.DEFAULT_USER_ID),
            project_id=project_id,
            team_id=tenancy.get_context().team_id,
            workspace=database.get_project_workspace(project_id),
            role="member",
            team_role="member",
            scopes=frozenset({"*"}),
            auth_type="scheduler",
            permission_mode=database.get_project_setting(
                project_id, "permission_mode", "strict"
            ),
        )
        token = tenancy.set_context(context)
        self._running.add(task_id)
        database.mark_scheduled_task_running(task_id)
        session_id = str(task["session_id"])
        before = database.get_session_messages(session_id)
        before_last = max((int(item.get("id") or 0) for item in before), default=0)
        answer_parts: list[str] = []
        rejected = 0
        error_message: str | None = None
        status = "OK"
        started = time.perf_counter()
        log.info("scheduled_task_started", task_id=task_id, session_id=session_id)

        try:
            async with asyncio.timeout(
                float(self._settings.scheduler_task_timeout_seconds)
            ):
                stream = self._runtime.run_stream(
                    session_id,
                    str(task["prompt"]),
                    approval_mode="auto_reject",
                    tenant=context,
                )
                async with aclosing(stream):
                    async for event in stream:
                        event_type = str(event.get("type") or "")
                        if event_type == "answer_chunk":
                            answer_parts.append(str(event.get("content") or ""))
                        elif event_type == "tool_result":
                            if "[自动拒绝]" in str(event.get("result") or ""):
                                rejected += 1
                        elif event_type == "error":
                            error_message = str(event.get("message") or "未知错误")
                            status = "ERROR"
                            break
        except TimeoutError:
            status = "ERROR"
            error_message = (
                "定时任务执行超时（"
                f"{self._settings.scheduler_task_timeout_seconds:g} 秒）"
            )
        except asyncio.CancelledError:
            status = "INTERRUPTED"
            error_message = "服务关闭，定时任务被中断"
            with suppress(Exception):
                database.finish_scheduled_task_run(
                    task_id, status, error_message
                )
            raise
        except Exception as exc:
            status = "ERROR"
            error_message = f"定时任务执行失败: {exc}"
            log.exception("scheduled_task_failed", task_id=task_id)
        finally:
            self._running.discard(task_id)
            tenancy.reset_context(token)

        after = database.get_session_messages(session_id)
        final_answer = next(
            (
                str(item.get("content") or "")
                for item in reversed(after)
                if int(item.get("id") or 0) > before_last
                and item.get("role") == "assistant"
                and str(item.get("content") or "").strip()
            ),
            "",
        )
        if not final_answer:
            final_answer = "".join(answer_parts).strip()

        notes: list[str] = []
        if rejected:
            status = "BLOCKED" if status == "OK" else status
            notes.append(
                f"无人值守模式自动拒绝了 {rejected} 个需要人工审批的高危操作。"
            )
        if error_message:
            notes.append(error_message)
        result = final_answer or "（本次运行没有生成文本回复）"
        if notes:
            result = result + "\n\n" + "\n".join(notes)
        result = truncate_text(result, self._settings.scheduler_result_max_chars)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        updated = database.finish_scheduled_task_run(task_id, status, result)
        log.info(
            "scheduled_task_finished",
            task_id=task_id,
            status=status,
            duration_ms=elapsed_ms,
            rejected=rejected,
        )
        return {
            "started": True,
            "task_id": task_id,
            "status": status,
            "duration_ms": elapsed_ms,
            "task": updated,
        }


scheduler = Scheduler()
