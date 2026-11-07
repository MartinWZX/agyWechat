"""Unified Configuration Management using Pydantic Settings & YAML Configuration."""

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # WeChat channel configuration
    WECHAT_CHANNEL: str = Field(
        default="webhook",
        description="WeChat channel mode: 'webhook' or 'ilink'",
    )
    WECHAT_TOKEN: SecretStr = Field(
        default=SecretStr("dev_token_123456"),
        description="WeChat Webhook verification token (masked via SecretStr)",
    )
    WECHAT_PUSH_URL: str = Field(
        default="http://127.0.0.1:8000/mock/wechat/push",
        description="WeChat Push API endpoint",
    )
    WECHAT_BOT_SECRET: SecretStr = Field(
        default=SecretStr(""),
        description="WeChat Push API authentication secret (masked via SecretStr)",
    )
    ALLOWED_USERS: List[str] = Field(
        default_factory=lambda: ["*"],
        description="List of allowed WeChat OpenIDs (* allows any user)",
    )
    ADMIN_USERS: List[str] = Field(
        default_factory=lambda: ["*"],
        description="List of admin WeChat user IDs for approval and wind control unlock",
    )

    # Tencent iLink Bot (Personal WeChat direct login) configuration
    WEIXIN_ACCOUNT_ID: Optional[str] = Field(
        default=None,
        description="Tencent iLink Bot account identifier (e.g. xxx@im.bot)",
    )
    WEIXIN_TOKEN: Optional[SecretStr] = Field(
        default=None,
        description="Tencent iLink Bot authentication token (masked via SecretStr)",
    )
    WEIXIN_BASE_URL: str = Field(
        default="https://ilinkai.weixin.qq.com",
        description="Tencent iLink API endpoint base URL",
    )

    # Host & Process configuration
    WORKSPACE_ROOT: Path = Field(
        default_factory=lambda: Path.cwd().resolve(),
        description="Root directory for the workspace",
    )
    AGY_BINARY: str = Field(
        default="agy",
        description="Absolute path or command name of the native Google agy binary",
    )
    AGY_LOCK_FILE: Path = Field(
        default_factory=lambda: Path.home() / ".agent_core" / "agy_host.lock",
        description="OS file lock to guarantee single persistent agy host process",
    )
    DB_PATH: Path = Field(
        default_factory=lambda: Path.home() / ".agent_core" / "core_state.db",
        description="SQLite database path for state, memory, rules and wind control",
    )

    PTY_COLS: int = Field(default=120, description="Virtual PTY columns")
    PTY_ROWS: int = Field(default=40, description="Virtual PTY rows")
    DEBOUNCE_SECONDS: float = Field(default=1.5, description="Terminal output debounce buffer interval")
    MAX_CHUNK_CHARS: int = Field(default=1500, description="Max chunk size for terminal push messages")
    WECHAT_MAX_MSG_LEN: int = Field(default=2000, description="Maximum characters per WeChat push message")

    # Anti-429 / Anti-403 Wind Control configuration (Iron Law 4)
    MIN_COOLDOWN_SECONDS: float = Field(default=12.0, description="Minimum cooldown between agy calls (>=12s)")
    SLIDING_WINDOW_5M_MAX: int = Field(default=6, description="Max calls in 5-minute sliding window (<=6)")
    SLIDING_WINDOW_1H_MAX: int = Field(default=30, description="Max calls in 1-hour sliding window (<=30)")
    JITTER_MIN_SECONDS: float = Field(default=1.5, description="Human typing cadence jitter min delay")
    JITTER_MAX_SECONDS: float = Field(default=4.8, description="Human typing cadence jitter max delay")
    QUIET_HOURS_START: str = Field(default="01:00", description="Quiet hours start time (HH:MM)")
    QUIET_HOURS_END: str = Field(default="07:00", description="Quiet hours end time (HH:MM)")
    COOLDOWN_429_SCHEDULE: List[int] = Field(
        default_factory=lambda: [120, 600, 1800],
        description="Exponential backoff steps for 429 rate limit (seconds)",
    )
    LOCKDOWN_403_HOURS: int = Field(default=24, description="Mandatory lockdown hours on 403 / ToS violation")

    # Service Bind
    SERVICE_HOST: str = Field(default="0.0.0.0", description="HTTP service host")
    SERVICE_PORT: int = Field(default=8000, description="HTTP service port")

    @field_validator("WEIXIN_TOKEN", mode="before")
    @classmethod
    def normalize_empty_weixin_token(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("ALLOWED_USERS", "ADMIN_USERS", mode="before")
    @classmethod
    def parse_user_list(cls, v: Any) -> List[str]:
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return ["*"]
            if v.startswith("[") and v.endswith("]"):
                try:
                    return json.loads(v)
                except Exception:
                    return [item.strip() for item in v.split(",") if item.strip()]
            return [item.strip() for item in v.split(",") if item.strip()]
        return ["*"]


@lru_cache()
def get_settings() -> Settings:
    """Return cached Settings instance."""
    return Settings()


def load_yaml_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    """Load configuration from config.yaml with fallback to empty dict."""
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}
