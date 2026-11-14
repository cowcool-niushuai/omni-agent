"""Model/provider registry.

Secrets are read from environment variables only. The JSON config stores
provider metadata (names, adapters, base URLs, model ids) and never API keys.

The schema is intentionally close to OpenCode's provider/model registry while
remaining backward compatible with the earlier flat ``models`` list.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from provider_adapters import (
    build_adapter,
    normalize_adapter,
    ollama_native_models,
)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(
    os.environ.get("OMNI_MODEL_CONFIG_PATH") or (BASE_DIR / "model_config.json")
).expanduser()
ENV_PATH = Path(os.environ.get("OMNI_ENV_PATH") or (BASE_DIR / ".env")).expanduser()
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
ENV_PATH.parent.mkdir(parents=True, exist_ok=True)

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
    "ollama": "OLLAMA_API_KEY",
    "lmstudio": "LMSTUDIO_API_KEY",
    "vllm": "VLLM_API_KEY",
}

LOCAL_PROVIDERS = {"ollama", "lmstudio", "vllm"}

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 2,
    "model": "deepseek/deepseek-v3",
    "active_model_id": "deepseek-v3",
    "providers": {
        "deepseek": {
            "name": "DeepSeek",
            "adapter": "openai-compatible",
            "base_url": "https://api.deepseek.com",
            "env": ["DEEPSEEK_API_KEY"],
            "models": {
                "deepseek-v3": {
                    "model_name": "deepseek-chat",
                    "label": "DeepSeek-V3",
                    "limit": {"context": 128000, "output": 8192},
                    "capabilities": {"tools": True},
                },
                "deepseek-r1": {
                    "model_name": "deepseek-reasoner",
                    "label": "DeepSeek-R1",
                    "limit": {"context": 128000, "output": 8192},
                    "capabilities": {"tools": True, "reasoning": True},
                },
            },
        },
        "openai": {
            "name": "OpenAI",
            "adapter": "openai-compatible",
            "base_url": "https://api.openai.com/v1",
            "env": ["OPENAI_API_KEY"],
            "models": {
                "gpt-4o": {
                    "model_name": "gpt-4o",
                    "label": "GPT-4o",
                    "limit": {"context": 128000, "output": 16384},
                    "capabilities": {"tools": True, "vision": True},
                },
            },
        },
        "anthropic": {
            "name": "Anthropic",
            "adapter": "anthropic",
            "base_url": "https://api.anthropic.com",
            "env": ["ANTHROPIC_API_KEY"],
            "models": {
                "claude-sonnet": {
                    "model_name": "claude-sonnet-4-20250514",
                    "label": "Claude Sonnet",
                    "limit": {"context": 200000, "output": 8192},
                    "capabilities": {"tools": True, "vision": True},
                },
            },
        },
        "ollama": {
            "name": "本地 Ollama",
            "adapter": "openai-compatible",
            "base_url": "http://localhost:11434/v1",
            "env": ["OLLAMA_API_KEY"],
            "models": {
                "ollama-qwen": {
                    "model_name": "qwen2.5-coder",
                    "label": "Qwen2.5-Coder (本地)",
                    "limit": {"context": 32768, "output": 8192},
                    "capabilities": {"tools": True},
                },
            },
        },
    },
}


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return slug[:32] or "provider"


def _default_env_name(provider_key: str) -> str:
    if provider_key in PROVIDER_ENV_KEYS:
        return PROVIDER_ENV_KEYS[provider_key]
    return f"CUSTOM_{_slug(provider_key).upper()}_API_KEY"


def provider_env_key(provider_key: str, provider: dict[str, Any] | None = None) -> str:
    """Return the environment variable name holding a provider's API key."""
    if provider:
        env_names = provider.get("env")
        if isinstance(env_names, list) and env_names:
            return str(env_names[0])
    return _default_env_name(provider_key)


def provider_secret(provider_key: str, provider: dict[str, Any] | None = None) -> str:
    """Read a provider secret from the environment; never from JSON."""
    env_names: list[str] = []
    if provider:
        configured = provider.get("env")
        if isinstance(configured, list):
            env_names.extend(str(item) for item in configured if item)
    if provider_key in PROVIDER_ENV_KEYS:
        env_names.append(PROVIDER_ENV_KEYS[provider_key])
    if not env_names:
        env_names.append(_default_env_name(provider_key))
    for env_name in env_names:
        value = os.getenv(env_name, "")
        if value:
            return value
    if provider_key in LOCAL_PROVIDERS:
        return "ollama"
    return ""


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
        if not isinstance(provider, dict):
            continue
        if "api_key" in provider:
            provider.pop("api_key", None)
            changed = True
    return changed


def _model_id(model_key: str, model: dict[str, Any]) -> str:
    return str(model.get("id") or model_key)


def _model_name(model_key: str, model: dict[str, Any]) -> str:
    return str(
        model.get("model_name")
        or model.get("modelID")
        or model.get("model_id")
        or model_key
    )


def _model_label(model_key: str, model: dict[str, Any]) -> str:
    return str(model.get("label") or model.get("name") or _model_name(model_key, model))


def _iter_models(provider: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    models = provider.get("models")
    result: list[tuple[str, dict[str, Any]]] = []
    if isinstance(models, dict):
        for key, model in models.items():
            if isinstance(model, dict):
                result.append((str(key), model))
    elif isinstance(models, list):
        for model in models:
            if not isinstance(model, dict):
                continue
            key = str(model.get("id") or model.get("model_name") or "")
            if key:
                result.append((key, model))
    return result


def _model_map(provider: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {key: model for key, model in _iter_models(provider)}


class ModelManager:
    def __init__(self) -> None:
        self.config: dict[str, Any] = {}
        self.load_config()

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------
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

        changed = self._normalize_config()
        if _strip_secrets(self.config):
            changed = True
        if changed:
            self.save_config()

    def _normalize_config(self) -> bool:
        """Migrate the old flat provider list into the registry schema."""
        changed = False
        providers = self.config.get("providers")
        if not isinstance(providers, dict):
            self.config["providers"] = {}
            providers = self.config["providers"]
            changed = True

        for provider_key, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            try:
                normalized = normalize_adapter(
                    str(provider.get("adapter") or ""), str(provider_key)
                )
            except ValueError:
                normalized = "openai-compatible"
            if normalized != provider.get("adapter"):
                provider["adapter"] = normalized
                changed = True
            if not isinstance(provider.get("env"), list) or not provider.get("env"):
                provider["env"] = [_default_env_name(str(provider_key))]
                changed = True

            models = provider.get("models")
            if isinstance(models, list):
                converted: dict[str, dict[str, Any]] = {}
                for model in models:
                    if not isinstance(model, dict):
                        continue
                    key = str(model.get("id") or model.get("model_name") or "")
                    if not key:
                        continue
                    entry = {k: v for k, v in model.items() if k != "id"}
                    entry.setdefault("model_name", key)
                    entry.setdefault("label", key)
                    converted[key] = entry
                provider["models"] = converted
                changed = True
            elif not isinstance(models, dict):
                provider["models"] = {}
                changed = True

        if self.config.get("schema_version") != 2:
            self.config["schema_version"] = 2
            changed = True

        if not self.config.get("model") and self.config.get("active_model_id"):
            try:
                provider_key, _, model_id, _ = self._find_model(
                    str(self.config["active_model_id"])
                )
                self.config["model"] = f"{provider_key}/{model_id}"
                changed = True
            except KeyError:
                pass
        return changed

    def save_config(self) -> None:
        self._normalize_config()
        _strip_secrets(self.config)
        tmp_path = CONFIG_PATH.parent / "model_config.tmp"
        tmp_path.write_text(
            json.dumps(self.config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(CONFIG_PATH)

    # ------------------------------------------------------------------
    # model lookup
    # ------------------------------------------------------------------
    def _find_model(self, selector: str) -> tuple[str, dict[str, Any], str, dict[str, Any]]:
        selector = selector.strip()
        providers = self.config.get("providers", {})
        if "/" in selector:
            provider_key, model_selector = selector.split("/", 1)
            provider = providers.get(provider_key)
            if isinstance(provider, dict):
                models = _model_map(provider)
                if model_selector in models:
                    return provider_key, provider, model_selector, models[model_selector]
                for model_key, model in models.items():
                    if _model_name(model_key, model) == model_selector:
                        return provider_key, provider, model_key, model
            raise KeyError(f"未知模型: {selector}")

        for provider_key, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            for model_key, model in _iter_models(provider):
                if model_key == selector or _model_name(model_key, model) == selector:
                    return str(provider_key), provider, model_key, model
        raise KeyError(f"未知模型: {selector}")

    def _info(
        self,
        provider_key: str,
        provider: dict[str, Any],
        model_id: str,
        model: dict[str, Any],
        *,
        include_secret: bool = False,
    ) -> dict[str, Any]:
        try:
            adapter = normalize_adapter(
                str(provider.get("adapter") or ""), provider_key
            )
        except ValueError:
            adapter = "openai-compatible"
        info: dict[str, Any] = {
            "id": model_id,
            "ref": f"{provider_key}/{model_id}",
            "label": _model_label(model_id, model),
            "model_name": _model_name(model_id, model),
            "provider_key": provider_key,
            "provider_name": provider.get("name", provider_key),
            "base_url": provider.get("base_url", ""),
            "adapter": adapter,
            "is_configured": bool(provider_secret(provider_key, provider)),
            "limit": model.get("limit") or {},
            "pricing": model.get("pricing") or {},
            "capabilities": model.get("capabilities") or {},
        }
        if include_secret:
            info["api_key"] = provider_secret(provider_key, provider)
            info["env"] = provider_env_key(provider_key, provider)
        return info

    def get_all_models(self) -> list[dict[str, Any]]:
        active = str(self.config.get("active_model_id") or "")
        models: list[dict[str, Any]] = []
        for provider_key, provider in self.config.get("providers", {}).items():
            if not isinstance(provider, dict):
                continue
            for model_id, model in _iter_models(provider):
                info = self._info(str(provider_key), provider, model_id, model)
                info["is_active"] = model_id == active or info["ref"] == active
                models.append(info)
        return models

    def get_active_model_info(self, *, include_secret: bool = False) -> dict[str, Any]:
        """Return the active model.

        The API key is included only when ``include_secret`` is true; API
        responses must never receive it.
        """
        selector = str(
            self.config.get("active_model_id") or self.config.get("model") or ""
        )
        try:
            provider_key, provider, model_id, model = self._find_model(selector)
        except KeyError:
            for provider_key, provider in self.config.get("providers", {}).items():
                if not isinstance(provider, dict):
                    continue
                entries = _iter_models(provider)
                if entries:
                    model_id, model = entries[0]
                    return self._info(
                        str(provider_key),
                        provider,
                        model_id,
                        model,
                        include_secret=include_secret,
                    )
            raise RuntimeError("No model providers configured") from None

        return self._info(
            provider_key,
            provider,
            model_id,
            model,
            include_secret=include_secret,
        )

    def get_model_info(
        self, selector: str | None = None, *, include_secret: bool = False
    ) -> dict[str, Any]:
        """Return one model by ``id`` or ``provider/id``; active when omitted."""
        if not selector:
            return self.get_active_model_info(include_secret=include_secret)
        try:
            provider_key, provider, model_id, model = self._find_model(selector)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        return self._info(
            provider_key,
            provider,
            model_id,
            model,
            include_secret=include_secret,
        )

    def set_active_model(self, model_selector: str) -> None:
        provider_key, _, model_id, _ = self._find_model(model_selector)
        self.config["active_model_id"] = model_id
        self.config["model"] = f"{provider_key}/{model_id}"
        self.save_config()

    # ------------------------------------------------------------------
    # provider/model editing
    # ------------------------------------------------------------------
    def update_provider_key(
        self,
        provider_key: str,
        api_key: str,
        base_url: str | None = None,
        *,
        adapter: str | None = None,
        env: list[str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        provider = self.config.get("providers", {}).get(provider_key)
        if not isinstance(provider, dict):
            raise KeyError(f"Unknown provider: {provider_key}")
        if env:
            provider["env"] = [str(item) for item in env if item]
        if adapter:
            provider["adapter"] = normalize_adapter(adapter, provider_key)
        if headers is not None:
            provider["headers"] = headers
        if api_key:
            _write_env_var(provider_env_key(provider_key, provider), api_key)
        if base_url:
            provider["base_url"] = base_url
        self.save_config()

    def add_custom_model(
        self,
        provider_name: str,
        base_url: str,
        api_key: str,
        model_name: str,
        label: str,
        *,
        adapter: str = "openai-compatible",
        provider_key: str | None = None,
        model_id: str | None = None,
        capabilities: dict[str, Any] | None = None,
        limit: dict[str, Any] | None = None,
        env_name: str | None = None,
    ) -> str:
        if not provider_name.strip() or not base_url.strip() or not model_name.strip():
            raise ValueError("厂商名称、Base URL 和模型标识不能为空")
        adapter = normalize_adapter(adapter, provider_key or provider_name)

        providers = self.config.setdefault("providers", {})
        pkey = (provider_key or "").strip()
        provider: dict[str, Any] | None = None
        if pkey and isinstance(providers.get(pkey), dict):
            provider = providers[pkey]
        if provider is None:
            if not pkey:
                pkey = f"custom_{_slug(provider_name)}_{os.urandom(2).hex()}"
            while pkey in providers:
                pkey = f"{pkey}_{os.urandom(2).hex()}"
            provider = {
                "name": provider_name.strip(),
                "adapter": adapter,
                "base_url": base_url.strip(),
                "env": [env_name or _default_env_name(pkey)],
                "models": {},
            }
            providers[pkey] = provider
        else:
            provider["name"] = provider_name.strip()
            provider["adapter"] = adapter
            provider["base_url"] = base_url.strip()
            if env_name:
                provider["env"] = [env_name]

        models = provider.get("models")
        if not isinstance(models, dict):
            models = _model_map(provider)
            provider["models"] = models

        mid = (model_id or "").strip()
        if not mid:
            mid = f"custom_{_slug(model_name)}_{os.urandom(2).hex()}"
        while mid in models:
            mid = f"{mid}_{os.urandom(2).hex()}"

        models[mid] = {
            "model_name": model_name.strip(),
            "label": label.strip() or model_name.strip(),
            "capabilities": capabilities or {"tools": True},
            "limit": limit or {},
        }
        if api_key:
            _write_env_var(provider_env_key(pkey, provider), api_key)
        self.config["active_model_id"] = mid
        self.config["model"] = f"{pkey}/{mid}"
        self.save_config()
        return mid

    # ------------------------------------------------------------------
    # discovery / probing
    # ------------------------------------------------------------------
    async def discover_models(self, provider_key: str) -> list[dict[str, Any]]:
        provider = self.config.get("providers", {}).get(provider_key)
        if not isinstance(provider, dict):
            raise KeyError(f"Unknown provider: {provider_key}")
        api_key = provider_secret(provider_key, provider)
        adapter = build_adapter(
            provider_key=provider_key,
            provider=provider,
            api_key=api_key,
        )
        try:
            discovered = await adapter.list_models()
        except Exception:
            if provider_key == "ollama" or "11434" in str(provider.get("base_url", "")):
                discovered = await asyncio.to_thread(
                    ollama_native_models,
                    str(provider.get("base_url") or ""),
                    api_key,
                )
            else:
                raise
        finally:
            await adapter.close()

        models = provider.get("models")
        if not isinstance(models, dict):
            models = _model_map(provider)
            provider["models"] = models

        existing_ids = set(models)
        existing_names = {_model_name(key, model) for key, model in models.items()}
        added: list[dict[str, Any]] = []
        for item in discovered:
            model_name = str(item.get("id") or "").strip()
            if not model_name or model_name in existing_ids or model_name in existing_names:
                continue
            model_id = model_name
            suffix = 1
            while model_id in models:
                suffix += 1
                model_id = f"{model_name}-{suffix}"
            models[model_id] = {
                "model_name": model_name,
                "label": str(item.get("name") or model_name),
                "capabilities": {"tools": True},
                "limit": {},
                "source": "discovered",
            }
            added.append(
                {
                    "id": model_id,
                    "ref": f"{provider_key}/{model_id}",
                    "name": str(item.get("name") or model_name),
                }
            )
        if added:
            self.save_config()
        return added

    async def test_provider(
        self,
        provider_key: str,
        *,
        base_url: str | None = None,
        adapter: str | None = None,
        api_key: str | None = None,
        model_name: str | None = None,
    ) -> dict[str, Any]:
        existing = self.config.get("providers", {}).get(provider_key)
        provider = dict(existing) if isinstance(existing, dict) else {
            "name": provider_key,
            "base_url": "",
            "env": [_default_env_name(provider_key)],
        }
        if base_url:
            provider["base_url"] = base_url
        if adapter:
            provider["adapter"] = normalize_adapter(adapter, provider_key)
        elif not provider.get("adapter"):
            provider["adapter"] = normalize_adapter("", provider_key)
        secret = (api_key or "").strip() or provider_secret(provider_key, provider)
        if not secret and provider_key not in LOCAL_PROVIDERS:
            return {
                "ok": False,
                "adapter": provider.get("adapter"),
                "error": f"缺少 API Key（{provider_env_key(provider_key, provider)}）",
            }

        started = time.perf_counter()
        try:
            probe = build_adapter(
                provider_key=provider_key,
                provider=provider,
                api_key=secret,
                model_info={"model_name": model_name or ""},
            )
            try:
                result = await probe.test_connection()
            finally:
                await probe.close()
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return result
        except Exception as exc:
            return {
                "ok": False,
                "adapter": provider.get("adapter"),
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": str(exc),
            }

    # ------------------------------------------------------------------
    # runtime clients
    # ------------------------------------------------------------------
    def get_client_and_model(self, model_selector: str | None = None) -> tuple[OpenAI, str]:
        info = self.get_model_info(model_selector, include_secret=True)
        if info.get("adapter") == "anthropic":
            raise RuntimeError(
                f"Provider '{info.get('provider_name')}' uses the native "
                "Anthropic adapter; use the async client."
            )
        api_key = info.get("api_key")
        if not api_key:
            env_name = provider_env_key(
                str(info.get("provider_key", "")),
                self.config.get("providers", {}).get(str(info.get("provider_key", ""))),
            )
            raise RuntimeError(
                f"Provider '{info.get('provider_name')}' has no API key configured. Add {env_name} to .env."
            )
        client = OpenAI(
            api_key=api_key,
            base_url=info.get("base_url") or "https://api.deepseek.com",
            timeout=35.0,
        )
        return client, str(info.get("model_name") or "deepseek-chat")

    def get_async_client_and_model(self, model_selector: str | None = None) -> tuple[Any, str]:
        info = self.get_model_info(model_selector, include_secret=True)
        provider_key = str(info.get("provider_key", ""))
        provider = self.config.get("providers", {}).get(provider_key)
        if not isinstance(provider, dict):
            raise RuntimeError(f"Provider config missing: {provider_key}")
        api_key = str(info.get("api_key") or "")
        if (
            not api_key
            and provider_key not in LOCAL_PROVIDERS
            and not provider.get("allow_no_key")
        ):
            env_name = provider_env_key(provider_key, provider)
            raise RuntimeError(
                f"Provider '{info.get('provider_name')}' has no API key configured. "
                f"Add {env_name} to .env."
            )
        adapter = build_adapter(
            provider_key=provider_key,
            provider=provider,
            api_key=api_key,
            model_info=info,
        )
        return adapter, str(info.get("model_name") or "deepseek-chat")


model_manager = ModelManager()

if __name__ == "__main__":
    active = model_manager.get_active_model_info()
    print("Active model:", active["label"], "from", active["provider_name"])
    print("Adapter:", active["adapter"])
    print("Available models:", len(model_manager.get_all_models()))
