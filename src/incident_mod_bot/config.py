from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

OpenAIImageDetail = Literal["low", "high", "auto"]


@dataclass(frozen=True)
class Settings:
    discord_token: str
    openai_api_key: str
    openai_model: str
    openai_image_detail: OpenAIImageDetail
    openai_max_image_dim: int
    db_path: str
    default_limit: int
    max_limit: int
    max_images_to_analyze: int
    mod_role_id: int | None
    debug_logs: bool
    auto_mod_default_channel_id: int | None


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value


def _env_optional(name: str) -> str | None:
    return os.getenv(name)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _parse_image_detail(value: str) -> OpenAIImageDetail:
    normalized = value.lower()
    if normalized not in {"low", "high", "auto"}:
        raise ValueError("OPENAI_IMAGE_DETAIL must be 'low', 'high', or 'auto'")
    return normalized  # type: ignore[return-value]


def load_settings() -> Settings:
    discord_token = _env_optional("DISCORD_TOKEN")
    openai_api_key = _env_optional("OPENAI_API_KEY")
    if not discord_token:
        raise ValueError("DISCORD_TOKEN is required")
    if not openai_api_key:
        raise ValueError("OPENAI_API_KEY is required")

    mod_role_value = _env_optional("MOD_ROLE_ID")
    auto_mod_default_channel_value = _env_optional("AUTO_MOD_DEFAULT_CHANNEL_ID")

    return Settings(
        discord_token=discord_token,
        openai_api_key=openai_api_key,
        openai_model=_env("OPENAI_MODEL", "gpt-4.1-mini"),
        openai_image_detail=_parse_image_detail(_env("OPENAI_IMAGE_DETAIL", "low")),
        openai_max_image_dim=_env_int("OPENAI_MAX_IMAGE_DIM", 512),
        db_path=_env("DB_PATH", "data/incident_bot.sqlite3"),
        default_limit=_env_int("DEFAULT_LIMIT", 50),
        max_limit=_env_int("MAX_LIMIT", 200),
        max_images_to_analyze=_env_int("MAX_IMAGES_TO_ANALYZE", 8),
        mod_role_id=int(mod_role_value) if mod_role_value else None,
        debug_logs=_env_bool("DEBUG_LOGS", False),
        auto_mod_default_channel_id=int(auto_mod_default_channel_value)
        if auto_mod_default_channel_value
        else None,
    )
