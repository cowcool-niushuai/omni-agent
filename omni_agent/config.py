"""Application configuration.

All settings come from environment variables (or a local .env file). Secrets
are never stored in source code or JSON config.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime settings, validated once at startup."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "OmniAgent"
    app_version: str = "0.1.0"
    environment: str = "dev"

    host: str = "127.0.0.1"
    port: int = 8001

    workspace_dir: Path = BASE_DIR / "workspace"
    data_dir: Path = BASE_DIR / "data"

    cors_origins: str = ""
    log_level: str = "INFO"
    log_json: bool = False
    max_iterations: int = 8

    # Context compaction: token budget before old messages are summarized.
    context_max_tokens: int = 24_000
    context_keep_last: int = 12

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [item.strip() for item in self.cors_origins.split(",") if item.strip()]
        if origins:
            return origins
        return ["http://127.0.0.1:8001", "http://localhost:8001"]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"prod", "production"}

    def ensure_directories(self) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
