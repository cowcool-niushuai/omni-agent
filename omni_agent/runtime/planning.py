"""Durable task planning helpers.

A plan is a small, explicit contract between the model and the runtime:

* every step has a stable id, a title, a status and optional acceptance criteria
* ``evidence`` records the concrete proof (test output, file path, URL, command)
* the verifier refuses to finish while steps are open or completed without proof

Plans live in LangGraph state, so checkpoints make them durable and resumable.
"""

from __future__ import annotations

import json
from typing import Any

from omni_agent.runtime.contracts import plan_is_satisfied

VALID_STATUS = {"pending", "in_progress", "completed", "blocked"}


def normalize_plan(arguments: dict[str, Any]) -> dict[str, Any]:
    goal = str(arguments.get("goal") or "").strip()
    done_when = str(arguments.get("done_when") or "").strip()
    raw_steps = arguments.get("steps") or []
    steps: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_steps[:20], start=1):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        step_id = str(raw.get("id") or f"step-{index}").strip()
        if step_id in seen:
            step_id = f"{step_id}-{index}"
        seen.add(step_id)
        status = str(raw.get("status") or "pending").strip()
        if status not in VALID_STATUS:
            status = "pending"
        steps.append(
            {
                "id": step_id,
                "title": title,
                "status": status,
                "acceptance": str(raw.get("acceptance") or "").strip(),
                "evidence": str(raw.get("evidence") or "").strip(),
            }
        )
    return {"goal": goal, "done_when": done_when, "steps": steps}


def plan_event(plan: dict[str, Any], *, source: str = "tool") -> dict[str, Any]:
    return {
        "type": "plan_update",
        "source": source,
        "goal": plan.get("goal", ""),
        "done_when": plan.get("done_when", ""),
        "steps": plan.get("steps", []),
    }


def render_for_prompt(plan: dict[str, Any] | None) -> str:
    if not plan or not plan.get("steps"):
        return ""
    lines = [f"目标：{plan.get('goal', '')}"]
    if plan.get("done_when"):
        lines.append(f"完成标准：{plan['done_when']}")
    for step in plan.get("steps", []):
        line = f"- [{step.get('status', 'pending')}] {step.get('title', '')}"
        if step.get("acceptance"):
            line += f" | 验收：{step['acceptance']}"
        if step.get("evidence"):
            line += f" | 证据：{step['evidence']}"
        lines.append(line)
    return "\n".join(lines)


def verification_issue(plan: dict[str, Any] | None) -> str:
    if not plan:
        return ""
    steps = list(plan.get("steps") or [])
    satisfied, reason = plan_is_satisfied(steps)
    if satisfied:
        return ""
    return reason


def compact_plan(plan: dict[str, Any] | None, max_chars: int = 4000) -> dict[str, Any] | None:
    """Bound checkpoint/SSE payload size while keeping the full structure."""
    if not plan:
        return None
    payload = json.dumps(plan, ensure_ascii=False)
    if len(payload) <= max_chars:
        return plan
    steps = plan.get("steps") or []
    return {
        "goal": str(plan.get("goal") or "")[:500],
        "done_when": str(plan.get("done_when") or "")[:500],
        "steps": steps[:20],
    }
