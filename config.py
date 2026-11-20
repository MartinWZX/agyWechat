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
    """Application settings loaded from `.env` and environment variables."""

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
    WEBHOOK_REQUIRE_SIGNATURE: bool = Field(
        default=False,
        description="Strictly require WeChat SHA-1 signature verification on /wechat/webhook",
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
        default_factory=lambda: ["admin_user"],
        description="Explicit admin WeChat user IDs",
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

    # Host & Engine binary configuration
    WORKSPACE_ROOT: Path = Field(
        default_factory=lambda: Path.cwd().resolve(),
        description="Root directory for the workspace",
    )
    AGY_BINARY: str = Field(default="agy", description="Path or command name of the agy CLI")
    PI_BINARY: str = Field(default="pi", description="Path or command name of the pi CLI")
    CLAUDE_BINARY: str = Field(default="claude", description="Path or command name of the claude CLI")
    OMP_BINARY: str = Field(default="omp", description="Path or command name of the omp CLI")
    DB_PATH: Path = Field(
        default=Path("memory/agent_core.db"),
        description="SQLite database path for working sessions and message history",
    )
    WECHAT_MAX_MSG_LEN: int = Field(
        default=2000,
        description="Maximum characters per outbound WeChat message segment",
    )
    PROGRESS_PUSH_INTERVAL_SECONDS: float = Field(
        default=0.0,
        description="Seconds between streaming intermediate CLI progress summary pushes (0 disables)",
    )
    USER_WORKSPACES: Dict[str, str] = Field(
        default_factory=dict,
        description="Optional per-user default workspace directory mapping (JSON object or user=path pairs)",
    )

    # Service Bind
    SERVICE_HOST: str = Field(default="0.0.0.0", description="HTTP service host")
    SERVICE_PORT: int = Field(default=8000, description="HTTP service port")

    @field_validator("WEIXIN_TOKEN", mode="before")
    @classmethod
    def normalize_empty_weixin_token(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("ALLOWED_USERS", mode="before")
    @classmethod
    def parse_allowed_users(cls, v: Any) -> List[str]:
        return _parse_str_list(v, default=["*"])

    @field_validator("ADMIN_USERS", mode="before")
    @classmethod
    def parse_admin_users(cls, v: Any) -> List[str]:
        return _parse_str_list(v, default=["admin_user"])

    @field_validator("USER_WORKSPACES", mode="before")
    @classmethod
    def parse_user_workspaces(cls, v: Any) -> Dict[str, str]:
        return _parse_str_dict(v)


def _parse_str_dict(v: Any) -> Dict[str, str]:
    if isinstance(v, dict):
        return {
            str(k).strip(): str(val).strip()
            for k, val in v.items()
            if str(k).strip() and str(val).strip()
        }
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return {}
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return {
                        str(k).strip(): str(val).strip()
                        for k, val in parsed.items()
                        if str(k).strip() and str(val).strip()
                    }
            except ValueError:
                pass
        result: Dict[str, str] = {}
        for part in s.split(","):
            if "=" in part:
                k, val = part.split("=", 1)
                k, val = k.strip(), val.strip()
                if k and val:
                    result[k] = val
        return result
    return {}


def _parse_str_list(v: Any, default: List[str]) -> List[str]:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return list(default)
        if v.startswith("[") and v.endswith("]"):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except ValueError:
                return [item.strip() for item in v.split(",") if item.strip()]
        return [item.strip() for item in v.split(",") if item.strip()]
    return list(default)


def resolve_setting_or_yaml(
    settings: Optional[Any],
    field_name: str,
    yaml_value: Any,
    fallback: Any,
) -> Any:
    """Resolve config value with precedence: explicit env/.env setting > config.yaml > default."""
    if settings is not None and field_name in getattr(settings, "model_fields_set", set()):
        val = getattr(settings, field_name, None)
        if val is not None and (not isinstance(val, str) or val.strip()):
            return val
    if yaml_value is not None and (not isinstance(yaml_value, str) or yaml_value.strip()):
        return yaml_value
    if settings is not None and hasattr(settings, field_name):
        val = getattr(settings, field_name)
        if val is not None:
            return val
    return fallback


def resolve_workspace_root(settings: Optional[Any] = None, yaml_config: Optional[Dict[str, Any]] = None) -> str:
    """Resolve workspace directory honoring explicit WORKSPACE_ROOT > config.yaml > cwd."""
    sys_cfg = (yaml_config or {}).get("system", {})
    raw = resolve_setting_or_yaml(settings, "WORKSPACE_ROOT", sys_cfg.get("workspace_dir"), str(Path.cwd().resolve()))
    return str(Path(str(raw)).expanduser().resolve())


def resolve_user_workspaces(
    settings: Optional[Any] = None, yaml_config: Optional[Dict[str, Any]] = None
) -> Dict[str, str]:
    """Merge per-user default workspace mappings with precedence: Settings.USER_WORKSPACES > config.yaml."""
    merged: Dict[str, str] = {}
    sys_cfg = (yaml_config or {}).get("system", {})
    yaml_map = sys_cfg.get("user_workspaces") if isinstance(sys_cfg, dict) else None
    if isinstance(yaml_map, dict):
        for uid, path_val in yaml_map.items():
            u = str(uid).strip()
            p = str(path_val).strip()
            if u and p:
                merged[u] = p
    if settings is not None and hasattr(settings, "USER_WORKSPACES"):
        env_map = getattr(settings, "USER_WORKSPACES")
        if isinstance(env_map, dict):
            for uid, path_val in env_map.items():
                u = str(uid).strip()
                p = str(path_val).strip()
                if u and p:
                    merged[u] = p
    return merged


def get_engine_binary(
    engine_id: str,
    settings: Optional[Any] = None,
    yaml_config: Optional[Dict[str, Any]] = None,
) -> str:
    """Resolve CLI binary for one of the four supported engines."""
    field_map = {
        "agy": "AGY_BINARY",
        "pi": "PI_BINARY",
        "claude": "CLAUDE_BINARY",
        "omp": "OMP_BINARY",
    }
    engines_cfg = (yaml_config or {}).get("engines", {})
    eng_cfg = engines_cfg.get(engine_id, {}) if isinstance(engines_cfg, dict) else {}
    return str(
        resolve_setting_or_yaml(
            settings,
            field_map.get(engine_id, ""),
            eng_cfg.get("binary") if isinstance(eng_cfg, dict) else None,
            engine_id,
        )
    )


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
