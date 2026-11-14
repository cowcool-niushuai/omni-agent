"""Sandboxed subprocess execution.

Backends
--------
``process``
    Runs the executable directly (no shell) with a scrubbed environment,
    resource limits (CPU / address space / file size / core) and a separate
    process group. This is process-level hardening, not a security boundary
    against a determined attacker on the host.
``bwrap``
    Wraps the command in bubblewrap: read-only system directories, a private
    ``/tmp``, a writable workspace bind, and no network by default. This is the
    container-style backend and is preferred when ``bwrap`` is installed.

The caller is responsible for policy decisions; this module only enforces
resource and isolation mechanics.
"""

from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

try:  # pragma: no cover - resource is POSIX-only
    import resource
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]

_SECRET_KEY = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.IGNORECASE)
_SAFE_ENV_KEYS = {
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "COLUMNS",
    "LINES",
}
_SYSTEM_RO_BINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt")


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool
    backend: str


def detect_backend(preferred: str = "auto") -> str:
    """Resolve ``auto`` to bubblewrap when available, else process."""
    choice = (preferred or "auto").lower()
    if choice == "process":
        return "process"
    if choice in {"auto", "bwrap"} and shutil.which("bwrap"):
        return "bwrap"
    return "process"


def minimal_env(cwd: Path) -> dict[str, str]:
    """Build a child environment without provider keys or credentials."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _SAFE_ENV_KEYS or (not _SECRET_KEY.search(key) and key in {"PYTHONPATH"}):
            env[key] = value
    home = cwd / ".home"
    tmp = cwd / ".tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PATH", os.defpath)
    return env


def _resource_limiter(
    memory_mb: int,
    cpu_seconds: int,
    file_mb: int,
) -> Callable[[], None] | None:
    if resource is None:
        return None

    def _apply() -> None:
        _set_limit(resource.RLIMIT_CORE, 0, 0)
        _set_limit(resource.RLIMIT_CPU, max(1, cpu_seconds), max(2, cpu_seconds + 1))
        _set_limit(
            resource.RLIMIT_AS,
            memory_mb * 1024 * 1024,
            memory_mb * 1024 * 1024,
        )
        _set_limit(
            resource.RLIMIT_FSIZE,
            file_mb * 1024 * 1024,
            file_mb * 1024 * 1024,
        )
        _set_limit(resource.RLIMIT_NOFILE, 256, 256)

    return _apply


def _set_limit(resource_id: int, soft: int, hard: int) -> None:
    if resource is None:
        return
    try:
        resource.setrlimit(resource_id, (soft, hard))
    except (ValueError, OSError):
        # A hard limit inherited from the parent may be lower; keep going.
        pass


def build_bwrap_argv(
    argv: Sequence[str],
    *,
    cwd: Path,
    network: bool,
    extra_ro_binds: Sequence[str] = (),
) -> list[str]:
    """Build a bubblewrap command line for ``argv``."""
    command = [
        "bwrap",
        "--die-with-parent",
        "--unshare-all",
        "--new-session",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    if network:
        command.append("--share-net")
    for path in _SYSTEM_RO_BINDS:
        if os.path.exists(path):
            command.extend(["--ro-bind", path, path])

    # The active Python virtualenv may live outside /usr (uv projects do).
    # Bind both the venv and the real interpreter installation, because
    # ``.venv/bin/python`` is often a symlink into ~/.local/share/uv.
    interpreter_roots: list[Path] = []
    for candidate in (
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(os.path.realpath(sys.executable)).parent.parent,
        Path(os.path.realpath(sys.executable)).parent.parent.parent,
    ):
        resolved = candidate.resolve()
        if resolved.exists() and not any(
            str(resolved).startswith(root) for root in _SYSTEM_RO_BINDS
        ):
            if resolved not in interpreter_roots:
                interpreter_roots.append(resolved)
    for root in interpreter_roots:
        command.extend(["--ro-bind", str(root), str(root)])

    for extra in extra_ro_binds:
        extra_path = Path(extra).expanduser()
        if extra_path.exists():
            command.extend(["--ro-bind", str(extra_path), str(extra_path)])

    command.extend(["--bind", str(cwd), str(cwd), "--chdir", str(cwd)])
    command.extend(["--", *argv])
    return command


def _collect_output(
    proc: subprocess.Popen[bytes],
    *,
    timeout: float,
    max_output_bytes: int,
) -> tuple[bytes, bytes, bool, bool]:
    """Read stdout/stderr with a deadline and a byte cap."""
    assert proc.stdout is not None and proc.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    last_data = time.monotonic()
    timed_out = False
    truncated = False
    open_streams = 2

    try:
        while open_streams > 0:
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                break
            if proc.poll() is not None and now - last_data > 0.2:
                # The child exited but a grandchild may still hold the pipes.
                break

            events = selector.select(timeout=min(0.1, max(0.0, deadline - now)))
            for key, _ in events:
                name = str(key.data)
                stream = proc.stdout if name == "stdout" else proc.stderr
                try:
                    chunk = os.read(stream.fileno(), 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    selector.unregister(stream)
                    open_streams -= 1
                    continue
                last_data = time.monotonic()
                buffer = buffers[name]
                room = max_output_bytes - len(buffer)
                if room > 0:
                    buffer.extend(chunk[:room])
                if len(chunk) > max(0, room):
                    truncated = True
    finally:
        selector.close()
        if timed_out:
            _kill_process_group(proc)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            proc.wait(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), timed_out, truncated


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path | str,
    timeout: float = 30.0,
    backend: str = "auto",
    network: bool = False,
    memory_mb: int = 512,
    cpu_seconds: int = 30,
    file_mb: int = 50,
    max_output_bytes: int = 256_000,
    extra_ro_binds: Sequence[str] = (),
) -> CommandResult:
    """Execute ``argv`` in the selected sandbox."""
    cwd_path = Path(cwd).resolve()
    cwd_path.mkdir(parents=True, exist_ok=True)
    if (backend or "").lower() == "host":
        resolved_backend = "host"
        actual = list(argv)
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        preexec = None
    else:
        resolved_backend = detect_backend(backend)
        if resolved_backend == "bwrap":
            actual = build_bwrap_argv(
                argv,
                cwd=cwd_path,
                network=network,
                extra_ro_binds=extra_ro_binds,
            )
        else:
            actual = list(argv)
        env = minimal_env(cwd_path)
        preexec = _resource_limiter(memory_mb, cpu_seconds, file_mb)
    started = time.perf_counter()
    try:
        proc = subprocess.Popen(
            actual,
            cwd=str(cwd_path),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
            preexec_fn=preexec,
        )
    except (OSError, ValueError) as exc:
        duration = int((time.perf_counter() - started) * 1000)
        return CommandResult(
            exit_code=None,
            stdout="",
            stderr=f"命令启动失败: {exc}",
            duration_ms=duration,
            timed_out=False,
            truncated=False,
            backend=resolved_backend,
        )

    stdout, stderr, timed_out, truncated = _collect_output(
        proc,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
    )
    duration = int((time.perf_counter() - started) * 1000)
    return CommandResult(
        exit_code=proc.returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        duration_ms=duration,
        timed_out=timed_out,
        truncated=truncated,
        backend=resolved_backend,
    )
