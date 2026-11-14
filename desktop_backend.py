"""Launcher for the standalone OmniAgent backend.

This module is the PyInstaller entry point.  It prepares a writable per-user
runtime directory, seeds default configuration on first run, and then starts
the FastAPI app.  It can also run the bundled SQLite MCP server, which lets the
packaged application use MCP without a system Python installation.
"""

from __future__ import annotations

import json
import os
import runpy
import shutil
import sys
from pathlib import Path

APP_NAME = "OmniAgent"


def _runtime_root() -> Path:
    """Directory containing bundled data files and Python modules."""
    if getattr(sys, "frozen", False):
        bundle = sys.__dict__.get("_MEIPASS")
        if bundle:
            return Path(str(bundle)).resolve()
    return Path(__file__).resolve().parent


def _default_user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


def _seed_file(target: Path, candidates: list[Path]) -> None:
    if target.exists():
        return
    for candidate in candidates:
        if candidate.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate, target)
            return


def _mcp_default() -> dict:
    root = _runtime_root()
    if getattr(sys, "frozen", False):
        command = sys.executable
        args = ["--mcp-sqlite"]
    else:
        command = sys.executable
        args = [str(root / "mcp_servers" / "sqlite_mcp.py")]
    return {
        "mcpServers": {
            "sqlite_database": {
                "command": command,
                "args": args,
                "description": "Query the OmniAgent SQLite database",
                "enabled": True,
            }
        }
    }


def prepare_runtime() -> Path:
    """Create writable directories and seed first-run configuration."""
    user_data = Path(
        os.environ.get("OMNI_DESKTOP_USER_DATA")
        or os.environ.get("OMNI_USER_DATA_DIR")
        or _default_user_data_dir()
    ).expanduser().resolve()
    data_dir = Path(os.environ.get("DATA_DIR") or user_data / "data").expanduser().resolve()
    workspace_dir = Path(
        os.environ.get("WORKSPACE_DIR") or user_data / "workspace"
    ).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    user_data.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("OMNI_DESKTOP", "1")
    os.environ.setdefault("OMNI_DESKTOP_USER_DATA", str(user_data))
    os.environ.setdefault("DATA_DIR", str(data_dir))
    os.environ.setdefault("WORKSPACE_DIR", str(workspace_dir))
    os.environ.setdefault("OMNI_DB_PATH", str(data_dir / "omni_agent.db"))
    os.environ.setdefault("OMNI_MODEL_CONFIG_PATH", str(user_data / "model_config.json"))
    os.environ.setdefault("OMNI_ENV_PATH", str(user_data / ".env"))
    os.environ.setdefault("OMNI_MCP_CONFIG_PATH", str(user_data / "mcp_config.json"))

    root = _runtime_root()
    _seed_file(
        Path(os.environ["OMNI_MODEL_CONFIG_PATH"]),
        [
            root / "model_config.example.json",
            Path(__file__).resolve().parent / "model_config.example.json",
        ],
    )
    mcp_path = Path(os.environ["OMNI_MCP_CONFIG_PATH"])
    if not mcp_path.exists():
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(
            json.dumps(_mcp_default(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return user_data


def _run_mcp_sqlite() -> None:
    script = _runtime_root() / "mcp_servers" / "sqlite_mcp.py"
    if not script.is_file():
        raise SystemExit(f"Bundled SQLite MCP server not found: {script}")
    runpy.run_path(str(script), run_name="__main__")


def _option(name: str) -> str | None:
    """Read ``--name value`` from the command line."""
    try:
        index = sys.argv.index(name)
    except ValueError:
        return None
    if index + 1 < len(sys.argv):
        return sys.argv[index + 1]
    return None


def serve() -> None:
    prepare_runtime()
    if "--mcp-sqlite" in sys.argv[1:]:
        _run_mcp_sqlite()
        return

    import uvicorn

    from server import app

    host = _option("--host") or os.environ.get("HOST", "127.0.0.1")
    port = int(_option("--port") or os.environ.get("PORT", "8001"))
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    serve()
