"""Sandbox execution tests."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from omni_agent.security.sandbox import detect_backend, run_command


def test_runs_without_shell(tmp_path: Path) -> None:
    result = run_command(
        [sys.executable, "-c", "print('sandbox-ok')"],
        cwd=tmp_path,
        timeout=10,
        backend="process",
    )
    assert result.exit_code == 0
    assert "sandbox-ok" in result.stdout
    assert result.backend == "process"


def test_environment_is_scrubbed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "leak-me-please")
    result = run_command(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('DEEPSEEK_API_KEY', '<missing>'))",
        ],
        cwd=tmp_path,
        timeout=10,
        backend="process",
    )
    assert "leak-me-please" not in result.stdout
    assert "<missing>" in result.stdout


def test_timeout_kills_process_group(tmp_path: Path) -> None:
    started = time.monotonic()
    result = run_command(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        timeout=0.4,
        backend="process",
    )
    elapsed = time.monotonic() - started
    assert result.timed_out is True
    assert elapsed < 4


def test_output_is_capped(tmp_path: Path) -> None:
    result = run_command(
        [sys.executable, "-c", "print('x' * 100000)"],
        cwd=tmp_path,
        timeout=10,
        max_output_bytes=1000,
        backend="process",
    )
    assert result.truncated is True
    assert len(result.stdout) <= 1000


def test_shell_metacharacters_are_literal(tmp_path: Path) -> None:
    result = run_command(
        ["echo", "a;b"],
        cwd=tmp_path,
        timeout=10,
        backend="process",
    )
    assert result.exit_code == 0
    assert "a;b" in result.stdout


def test_backend_detection() -> None:
    assert detect_backend("process") == "process"
    assert detect_backend("auto") in {"process", "bwrap"}
    assert detect_backend("bwrap") in {"process", "bwrap"}
