"""Configuration management using pydantic-settings."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional, Union
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # WeChat credentials & configuration
    WECHAT_CHANNEL: str = Field(
        default="webhook",
        description="WeChat channel mode: 'webhook' (default) or 'ilink' (direct personal WeChat)",
    )
    WECHAT_TOKEN: str = Field(default="dev_token_123456", description="WeChat Webhook verification token")
    WECHAT_PUSH_URL: str = Field(
        default="http://127.0.0.1:8000/mock/wechat/push",
        description="WeChat ClawBot active push API endpoint",
    )
    WECHAT_BOT_SECRET: str = Field(default="", description="WeChat Push API authentication secret")
    ALLOWED_USERS: List[str] = Field(
        default_factory=lambda: ["*"],
        description="List of allowed WeChat OpenIDs (* allows any user)",
    )

    # Tencent iLink Bot (Personal WeChat direct login) configuration
    WEIXIN_ACCOUNT_ID: Optional[str] = Field(
        default=None,
        description="Tencent iLink Bot account identifier (e.g. xxx@im.bot)",
    )
    WEIXIN_TOKEN: Optional[str] = Field(
        default=None,
        description="Tencent iLink Bot authentication token",
    )
    WEIXIN_BASE_URL: str = Field(
        default="https://ilinkai.weixin.qq.com",
        description="Tencent iLink API endpoint base URL",
    )

    # Sandbox & executable
    WORKSPACE_ROOT: Path = Field(
        default_factory=lambda: Path.cwd().resolve(),
        description="Root path for file sandbox; /cd cannot escape this directory",
    )
    AGY_BINARY: str = Field(default="agy", description="Path to agy executable")
    AGY_DEFAULT_ARGS: List[str] = Field(
        default_factory=list,
        description="Default arguments to pass to agy process",
    )

    # Terminal & PTY configurations
    PTY_COLS: int = Field(default=120, description="Virtual PTY columns")
    PTY_ROWS: int = Field(default=40, description="Virtual PTY rows")

    # Cleaner and debounce thresholds
    DEBOUNCE_SECONDS: float = Field(default=1.5, description="Debounce aggregate window in seconds")
    MAX_CHUNK_CHARS: int = Field(default=1500, description="Target flush chunk character size")
    WECHAT_MAX_MSG_LEN: int = Field(default=2000, description="Hard maximum message size for WeChat")

    # FastAPI Server configuration
    SERVICE_HOST: str = Field(default="0.0.0.0", description="Service host to bind")
    SERVICE_PORT: int = Field(default=8000, description="Service port to bind")

    @field_validator("ALLOWED_USERS", mode="before")
    @classmethod
    def parse_allowed_users(cls, v: Any) -> List[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v]
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed]
                except Exception:
                    pass
            return [x.strip() for x in v.split(",") if x.strip()]
        return []

    @field_validator("AGY_DEFAULT_ARGS", mode="before")
    @classmethod
    def parse_default_args(cls, v: Any) -> List[str]:
        if isinstance(v, list):
            return [str(x) for x in v]
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        return [str(x) for x in parsed]
                except Exception:
                    pass
            return [x.strip() for x in v.split() if x.strip()]
        return []

    @field_validator("WORKSPACE_ROOT", mode="after")
    @classmethod
    def resolve_workspace_root(cls, v: Path) -> Path:
        resolved = v.resolve()
        if not resolved.exists():
            resolved.mkdir(parents=True, exist_ok=True)
        return resolved


@lru_cache()
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
