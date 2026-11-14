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


def test_registry_exposes_ref_and_adapter(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    mm = build_manager()
    model = mm.get_all_models()[0]
    assert model["ref"] == "deepseek/test-model"
    assert model["adapter"] == "openai-compatible"
    assert model["capabilities"] == {}
    assert model["limit"] == {}


def test_set_active_model_accepts_provider_ref(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(model_manager, "CONFIG_PATH", tmp_path / "model_config.json")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    mm = build_manager()
    mm.set_active_model("deepseek/test-model")
    assert mm.config["active_model_id"] == "test-model"
    assert mm.config["model"] == "deepseek/test-model"
    saved = (tmp_path / "model_config.json").read_text(encoding="utf-8")
    assert '"schema_version": 2' in saved


def test_add_custom_anthropic_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(model_manager, "CONFIG_PATH", tmp_path / "model_config.json")
    mm = build_manager()
    model_id = mm.add_custom_model(
        "Claude 代理",
        "https://proxy.example.com",
        "",
        "claude-sonnet",
        "Claude Sonnet Proxy",
        adapter="anthropic",
    )
    info = mm.get_active_model_info()
    assert info["id"] == model_id
    assert info["adapter"] == "anthropic"
    assert info["ref"].endswith(model_id)
