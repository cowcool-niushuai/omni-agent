from typing import Any

import pytest

import model_manager as mm_module


class FakeAdapter:
    def __init__(self, *, models: list[dict[str, Any]] | None = None) -> None:
        self.models = models or []

    async def list_models(self) -> list[dict[str, Any]]:
        return self.models

    async def test_connection(self) -> dict[str, Any]:
        return {"ok": True, "adapter": "fake", "models": ["fake-model"]}

    async def close(self) -> None:
        return None


def make_manager(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> mm_module.ModelManager:
    monkeypatch.setattr(mm_module, "CONFIG_PATH", tmp_path / "model_config.json")
    manager = mm_module.ModelManager()
    manager.config = {
        "schema_version": 2,
        "active_model_id": "test",
        "providers": {
            "deepseek": {
                "name": "DeepSeek",
                "adapter": "openai-compatible",
                "base_url": "https://api.deepseek.com",
                "env": ["DEEPSEEK_API_KEY"],
                "models": {
                    "test": {"model_name": "deepseek-chat", "label": "Test"},
                },
            }
        },
    }
    return manager


async def test_discover_models_adds_new_entries(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    manager = make_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mm_module,
        "build_adapter",
        lambda **_: FakeAdapter(models=[{"id": "deepseek-chat"}, {"id": "deepseek-new"}]),
    )

    added = await manager.discover_models("deepseek")
    assert [item["id"] for item in added] == ["deepseek-new"]
    assert manager.config["providers"]["deepseek"]["models"]["deepseek-new"]["model_name"] == "deepseek-new"


async def test_probe_provider_reports_ok(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    manager = make_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(mm_module, "build_adapter", lambda **_: FakeAdapter())

    result = await manager.test_provider("deepseek")
    assert result["ok"] is True
    assert result["latency_ms"] >= 0


async def test_probe_provider_without_key_reports_error(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    manager = make_manager(tmp_path, monkeypatch)
    result = await manager.test_provider("deepseek")
    assert result["ok"] is False
    assert "API Key" in result["error"]