"""Model/provider registry.

Secrets are read from environment variables only. The JSON config stores
provider metadata (names, base URLs, model ids) and never API keys.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "model_config.json"
ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH)

PROVIDER_ENV_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
    "google": "GOOGLE_API_KEY",
    "siliconflow": "SILICONFLOW_API_KEY",
}

LOCAL_PROVIDERS = {"ollama"}

DEFAULT_CONFIG: dict[str, Any] = {
    "active_model_id": "deepseek-v3",
    "providers": {
        "deepseek": {
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com",
            "models": [
                {"id": "deepseek-v3", "model_name": "deepseek-chat", "label": "DeepSeek-V3"},
                {"id": "deepseek-r1", "model_name": "deepseek-reasoner", "label": "DeepSeek-R1"},
            ],
        },
        "openai": {
            "name": "OpenAI",
            "base_url": "https://api.openai.com/v1",
            "models": [
                {"id": "gpt-4o", "model_name": "gpt-4o", "label": "GPT-4o"},
                {"id": "gpt-4o-mini", "model_name": "gpt-4o-mini", "label": "GPT-4o Mini"},
            ],
        },
        "ollama": {
            "name": "本地 Ollama",
            "base_url": "http://localhost:11434/v1",
            "models": [
                {"id": "ollama-qwen", "model_name": "qwen2.5-coder", "label": "Qwen2.5-Coder (本地)"},
            ],
        },
    },
}


def provider_env_key(provider_key: str) -> str:
    """Return the environment variable name holding a provider's API key."""
    if provider_key in PROVIDER_ENV_KEYS:
        return PROVIDER_ENV_KEYS[provider_key]
    suffix = provider_key.removeprefix("custom_").upper()
    return f"CUSTOM_{suffix}_API_KEY"


def provider_secret(provider_key: str) -> str:
    """Read a provider secret from the environment; never from JSON."""
    if provider_key in LOCAL_PROVIDERS:
        return "ollama"
    return os.getenv(provider_env_key(provider_key), "")


def _write_env_var(name: str, value: str) -> None:
    """Persist a secret to the local .env file with owner-only permissions."""
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{name}="):
            lines[index] = f"{name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name}={value}")

    tmp_path = ENV_PATH.parent / ".env.tmp"
    tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(ENV_PATH)
    os.environ[name] = value


def _strip_secrets(config: dict[str, Any]) -> bool:
    """Remove any api_key fields from config; returns True when changed."""
    changed = False
    for provider in config.get("providers", {}).values():
        if "api_key" in provider:
            provider.pop("api_key", None)
            changed = True
    return changed


class ModelManager:
    def __init__(self) -> None:
        self.config: dict[str, Any] = {}
        self.load_config()

    def load_config(self) -> None:
        if not CONFIG_PATH.exists():
            self.config = json.loads(json.dumps(DEFAULT_CONFIG))
            self.save_config()
            return

        try:
            with open(CONFIG_PATH, encoding="utf-8") as handle:
                self.config = json.load(handle)
        except (OSError, json.JSONDecodeError):
            self.config = json.loads(json.dumps(DEFAULT_CONFIG))

        if _strip_secrets(self.config):
            self.save_config()

    def save_config(self) -> None:
        _strip_secrets(self.config)
        tmp_path = CONFIG_PATH.parent / "model_config.tmp"
        tmp_path.write_text(
            json.dumps(self.config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(CONFIG_PATH)

    def get_all_models(self) -> list[dict[str, Any]]:
        active_id = self.config.get("active_model_id", "")
        models: list[dict[str, Any]] = []
        for provider_key, provider in self.config.get("providers", {}).items():
            for model in provider.get("models", []):
                models.append(
                    {
                        "id": model["id"],
                        "label": model["label"],
                        "model_name": model["model_name"],
                        "provider_key": provider_key,
                        "provider_name": provider.get("name", provider_key),
                        "base_url": provider.get("base_url", ""),
                        "is_active": model["id"] == active_id,
                        "is_configured": bool(provider_secret(provider_key)),
                    }
                )
        return models

    def get_active_model_info(self, *, include_secret: bool = False) -> dict[str, Any]:
        """Return the active model.

        The API key is included only when ``include_secret`` is true; API
        responses must never receive it.
        """
        active_id = self.config.get("active_model_id", "")
        providers = self.config.get("providers", {})

        for provider_key, provider in providers.items():
            for model in provider.get("models", []):
                if model["id"] == active_id:
                    info: dict[str, Any] = {
                        "id": model["id"],
                        "label": model["label"],
                        "model_name": model["model_name"],
                        "provider_key": provider_key,
                        "provider_name": provider.get("name", provider_key),
                        "base_url": provider.get("base_url", ""),
                        "is_configured": bool(provider_secret(provider_key)),
                    }
                    if include_secret:
                        info["api_key"] = provider_secret(provider_key)
                    return info

        for provider_key, provider in providers.items():
            models = provider.get("models", [])
            if not models:
                continue
            model = models[0]
            info = {
                "id": model["id"],
                "label": model["label"],
                "model_name": model["model_name"],
                "provider_key": provider_key,
                "provider_name": provider.get("name", provider_key),
                "base_url": provider.get("base_url", ""),
                "is_configured": bool(provider_secret(provider_key)),
            }
            if include_secret:
                info["api_key"] = provider_secret(provider_key)
            return info

        raise RuntimeError("No model providers configured")

    def set_active_model(self, model_id: str) -> None:
        self.config["active_model_id"] = model_id
        self.save_config()

    def update_provider_key(self, provider_key: str, api_key: str, base_url: str | None = None) -> None:
        if provider_key not in self.config.get("providers", {}):
            raise KeyError(f"Unknown provider: {provider_key}")
        if api_key:
            _write_env_var(provider_env_key(provider_key), api_key)
        if base_url:
            self.config["providers"][provider_key]["base_url"] = base_url
        self.save_config()

    def add_custom_model(
        self,
        provider_name: str,
        base_url: str,
        api_key: str,
        model_name: str,
        label: str,
    ) -> str:
        provider_key = "custom_" + os.urandom(3).hex()
        model_id = "custom_" + os.urandom(3).hex()
        self.config.setdefault("providers", {})[provider_key] = {
            "name": provider_name,
            "base_url": base_url,
            "models": [{"id": model_id, "model_name": model_name, "label": label}],
        }
        if api_key:
            _write_env_var(provider_env_key(provider_key), api_key)
        self.config["active_model_id"] = model_id
        self.save_config()
        return model_id

    def get_client_and_model(self) -> tuple[OpenAI, str]:
        info = self.get_active_model_info(include_secret=True)
        api_key = info.get("api_key")
        if not api_key:
            env_name = provider_env_key(str(info.get("provider_key", "")))
            raise RuntimeError(
                f"Provider '{info.get('provider_name')}' has no API key configured. Add {env_name} to .env."
            )
        client = OpenAI(
            api_key=api_key,
            base_url=info.get("base_url") or "https://api.deepseek.com",
            timeout=35.0,
        )
        return client, str(info.get("model_name") or "deepseek-chat")


model_manager = ModelManager()

if __name__ == "__main__":
    active = model_manager.get_active_model_info()
    print("Active model:", active["label"], "from", active["provider_name"])
    print("Available models:", len(model_manager.get_all_models()))
