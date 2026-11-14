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

    # Phase 3: local memory / RAG.
    memory_injection_limit: int = 6
    memory_always_include: int = 3
    memory_search_limit: int = 5
    memory_max_content_chars: int = 8_000
    knowledge_search_limit: int = 5
    knowledge_max_content_chars: int = 20_000

    # Phase 2: sandboxed execution and tool quotas.
    sandbox_backend: str = "auto"  # auto | bwrap | process
    sandbox_network: bool = False
    sandbox_extra_ro_binds: str = ""
    command_timeout_seconds: float = 30.0
    command_max_memory_mb: int = 512
    command_max_cpu_seconds: int = 30
    command_max_file_mb: int = 50
    command_max_processes: int = 64
    command_max_output_bytes: int = 256_000
    command_policy_mode: str = "strict"  # strict | permissive
    python_eval_timeout_seconds: float = 30.0

    tool_timeout_seconds: float = 60.0
    subagent_timeout_seconds: float = 300.0
    tool_max_output_chars: int = 12_000
    tool_max_concurrency: int = 4
    max_file_write_bytes: int = 10 * 1024 * 1024

    web_fetch_timeout_seconds: float = 10.0
    web_fetch_max_bytes: int = 2_000_000
    web_fetch_max_redirects: int = 3
    web_fetch_allowed_ports: str = "80,443,8080,8443"
    allow_private_network: bool = False

    # Phase 3: headless browser automation.
    browser_enabled: bool = True
    browser_headless: bool = True
    browser_timeout_seconds: float = 20.0
    browser_launch_timeout_seconds: float = 30.0
    browser_max_text_chars: int = 8_000
    browser_screenshot_max_bytes: int = 5 * 1024 * 1024
    browser_viewport_width: int = 1_280
    browser_viewport_height: int = 800
    browser_user_agent: str = ""
    browser_max_wait_ms: int = 10_000
    browser_proxy: str = ""
    browser_proxy_bypass: str = ""

    mcp_request_timeout_seconds: float = 15.0
    audit_enabled: bool = True

    # Phase 3: unattended scheduled tasks.
    scheduler_enabled: bool = True
    scheduler_poll_seconds: float = 15.0
    scheduler_task_timeout_seconds: float = 600.0
    scheduler_min_interval_seconds: int = 60
    scheduler_max_interval_seconds: int = 604_800
    scheduler_name_max_chars: int = 120
    scheduler_prompt_max_chars: int = 20_000
    scheduler_result_max_chars: int = 4_000
    scheduler_max_due_per_poll: int = 5
    scheduler_max_tasks: int = 100

    @property
    def cors_origin_list(self) -> list[str]:
        origins = [item.strip() for item in self.cors_origins.split(",") if item.strip()]
        if origins:
            return origins
        return ["http://127.0.0.1:8001", "http://localhost:8001"]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"prod", "production"}

    @property
    def sandbox_ro_bind_list(self) -> list[str]:
        return [item.strip() for item in self.sandbox_extra_ro_binds.split(",") if item.strip()]

    @property
    def web_fetch_port_list(self) -> set[int]:
        ports: set[int] = set()
        for item in self.web_fetch_allowed_ports.split(","):
            item = item.strip()
            if item.isdigit():
                ports.add(int(item))
        return ports or {80, 443}

    def ensure_directories(self) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
