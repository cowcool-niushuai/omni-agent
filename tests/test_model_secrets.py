import model_manager


def build_manager() -> model_manager.ModelManager:
    mm = model_manager.ModelManager()
    mm.config = {
        "active_model_id": "test-model",
        "providers": {
            "deepseek": {
                "name": "DeepSeek",
                "base_url": "https://api.deepseek.com",
                "models": [
                    {"id": "test-model", "model_name": "deepseek-chat", "label": "Test"},
                ],
            }
        },
    }
    return mm


def test_active_model_hides_secret_by_default(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    info = build_manager().get_active_model_info()
    assert info["id"] == "test-model"
    assert "api_key" not in info


def test_active_model_exposes_secret_only_on_request(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    info = build_manager().get_active_model_info(include_secret=True)
    assert info["api_key"] == "unit-test-key"


def test_model_list_never_contains_secret(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    models = build_manager().get_all_models()
    assert models
    assert all("api_key" not in model for model in models)


def test_save_config_never_writes_secret(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(model_manager, "CONFIG_PATH", tmp_path / "model_config.json")
    mm = build_manager()
    mm.config["providers"]["deepseek"]["api_key"] = "leaked"
    mm.save_config()
    assert "leaked" not in (tmp_path / "model_config.json").read_text(encoding="utf-8")
