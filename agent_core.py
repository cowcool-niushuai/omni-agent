"""Compatibility facade for the old ``agent_core`` import path.

The actual implementation lives in :mod:`omni_agent.runtime.graph`.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from omni_agent.runtime.graph import AgentRuntime, runtime


class OmniAgentCore:
    """Thin wrapper kept so existing server/CLI imports keep working."""

    def __init__(self, runtime_instance: AgentRuntime | None = None) -> None:
        self.runtime = runtime_instance or runtime

    async def initialize(self) -> None:
        await self.runtime.initialize()

    async def close(self) -> None:
        await self.runtime.close()

    async def run_react_stream(
        self,
        session_id: str,
        user_message: str,
        max_iterations: int = 5,
        *,
        approval_mode: str = "human",
    ) -> AsyncGenerator[dict[str, Any], None]:
        async for event in self.runtime.run_react_stream(
            session_id,
            user_message,
            max_iterations=max_iterations,
            approval_mode=approval_mode,
        ):
            yield event

    async def resume_stream(
        self,
        session_id: str,
        request_id: str,
        action: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        async for event in self.runtime.resume_stream(
            session_id, request_id, action
        ):
            yield event

    async def run_subagent_task(self, session_id: str, role: str, task: str) -> str:
        return await self.runtime.run_subagent_task(session_id, role, task)

    async def delete_thread(self, session_id: str) -> None:
        await self.runtime.delete_thread(session_id)


agent_core = OmniAgentCore()
