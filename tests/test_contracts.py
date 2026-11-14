"""Tool contract and planning/verifier unit tests."""

from __future__ import annotations

import pytest

from omni_agent.runtime import contracts, planning


def test_native_arguments_are_normalized() -> None:
    args, error = contracts.validate_tool_call(
        "file_read", {"filepath": "notes.txt", "start_line": "2"}
    )
    assert error is None
    assert args["start_line"] == 2
    assert args["end_line"] == 200


def test_native_arguments_reject_unknown_fields() -> None:
    with pytest.raises(contracts.ToolContractError) as exc:
        contracts.validate_tool_call("bash_run", {"command": "ls", "nonsense": 1})
    assert exc.value.tool_name == "bash_run"
    assert "nonsense" in exc.value.detail


def test_mcp_arguments_are_passed_through() -> None:
    args, error = contracts.validate_tool_call(
        "mcp_demo", {"query": "select 1", "unknown": True}
    )
    assert error is None
    assert args["unknown"] is True


def test_output_classification() -> None:
    assert contracts.classify_output("file_read", "hello").ok is True
    blocked = contracts.classify_output("bash_run", "[安全策略拒绝] 禁止 rm -rf")
    assert blocked.ok is False and blocked.error_code == "BLOCKED"
    failed = contracts.classify_output("file_read", "文件不存在: x")
    assert failed.ok is False and failed.error_code == "TOOL_ERROR"
    timeout = contracts.classify_output("bash_run", "[工具超时] 超时")
    assert timeout.retryable is True


def test_idempotent_policy() -> None:
    assert contracts.is_idempotent("file_read", {}) is True
    assert contracts.is_idempotent("file_write", {}) is False
    assert contracts.is_idempotent("browser", {"action": "text"}) is True
    assert contracts.is_idempotent("browser", {"action": "click"}) is False


def test_plan_requires_completed_steps_with_evidence() -> None:
    plan = planning.normalize_plan(
        {
            "goal": "ship it",
            "done_when": "tests pass",
            "steps": [
                {"title": "write", "status": "completed", "evidence": "file.py"},
                {"title": "test", "status": "pending"},
            ],
        }
    )
    assert planning.verification_issue(plan)

    plan["steps"][1].update({"status": "completed", "evidence": "pytest 10 passed"})
    assert planning.verification_issue(plan) == ""

    plan["steps"][0]["evidence"] = ""
    assert "缺少验证证据" in planning.verification_issue(plan)


def test_plan_markdown_for_prompt() -> None:
    plan = planning.normalize_plan(
        {"goal": "goal", "steps": [{"title": "one", "status": "in_progress"}]}
    )
    rendered = planning.render_for_prompt(plan)
    assert "goal" in rendered
    assert "[in_progress] one" in rendered
