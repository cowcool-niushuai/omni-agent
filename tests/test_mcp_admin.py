"""Regression tests for the local MCP configuration manager."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import mcp_config


class FakeManager:
    def __init__(self) -> None:
        self.clients: dict[str, Any] = {}
        self.reloads = 0

    def load_config(self) -> None:
        self.reloads += 1

    def list_server_status(self) -> list[dict[str, Any]]:
        return []


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "mcp_config.json"
    monkeypatch.setattr(mcp_config, "MCP_CONFIG_FILE", str(path))
    return path


def test_validate_server_normalizes_and_rejects_bad_input() -> None:
    clean = mcp_config.validate_server(
        "local-app",
        "/usr/bin/python3",
        args=[1, "server.py"],
        env={"TOKEN": 123},
    )
    assert clean == {
        "command": "/usr/bin/python3",
        "args": ["1", "server.py"],
        "env": {"TOKEN": "123"},
        "cwd": "",
    }
    with pytest.raises(ValueError, match="名称"):
        mcp_config.validate_server("bad name!", "python")
    with pytest.raises(ValueError, match="启动命令"):
        mcp_config.validate_server("ok", "")


def test_upsert_list_toggle_and_delete(config_file: Path) -> None:
    manager = FakeManager()
    created = mcp_config.upsert(
        manager,
        "local-app",
        command="/usr/bin/python3",
        args=["server.py"],
        description="local test",
        enabled=True,
    )
    assert created["name"] == "local-app"
    assert created["enabled"] is True
    assert manager.reloads == 1

    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["mcpServers"]["local-app"]["command"] == "/usr/bin/python3"

    updated = mcp_config.upsert(
        manager,
        "local-app",
        command="/usr/bin/python3",
        args=["server.py"],
        description="updated",
        enabled=False,
    )
    assert updated["enabled"] is False
    assert updated["status"] == "disabled"
    assert len(mcp_config.list_configs(manager)) == 1

    assert mcp_config.delete(manager, "local-app") is True
    assert mcp_config.delete(manager, "local-app") is False
    assert mcp_config.list_configs(manager) == []


def test_test_server_reports_tools(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.stopped = False

        def list_tools(self) -> list[dict[str, str]]:
            return [{"name": "ping"}, {"name": "echo"}]

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(mcp_config, "MCPClient", FakeClient)
    result = mcp_config.test_server(FakeManager(), command="/usr/bin/python3")
    assert result["ok"] is True
    assert result["tools"] == ["ping", "echo"]
    assert result["tools_count"] == 2


def test_reload_preserves_disabled_entries(config_file: Path) -> None:
    config_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "disabled-app": {
                        "command": "/usr/bin/python3",
                        "args": ["server.py"],
                        "enabled": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    manager = FakeManager()
    result = mcp_config.reload(manager)
    assert manager.reloads == 1
    assert result[0]["name"] == "disabled-app"
    assert result[0]["enabled"] is False
