from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

OpenAIImageDetail = Literal["low", "high", "auto"]

# The bot is a member of servers it is only meant to read, so acting is limited to
# the servers named here rather than to every server it can see.
DEFAULT_ACTIVE_GUILD_IDS = frozenset({174993814845521922})


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
    active_guild_ids: frozenset[int]
    # A user pinging the modmail bot's own account directly (rather than
    # opening a thread) is the same kind of thing a role ping is - someone
    # needs a mod and doesn't know the right way to ask. Treated the same
    # as an @-role trigger once the account id is configured here.
    modmail_bot_user_ids: frozenset[int] = field(default_factory=frozenset)

    def is_active_guild(self, guild_id: int | None) -> bool:
        """Whether the bot should act on something that happened in this server."""
        if guild_id is None:
            return False
        return guild_id in self.active_guild_ids


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


def _parse_guild_ids(raw: str | None) -> frozenset[int]:
    if raw is None:
        return DEFAULT_ACTIVE_GUILD_IDS
    return frozenset(int(part) for part in raw.split(",") if part.strip())


def _parse_user_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    return frozenset(int(part) for part in raw.split(",") if part.strip())


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
        active_guild_ids=_parse_guild_ids(_env_optional("ACTIVE_GUILD_IDS")),
        modmail_bot_user_ids=_parse_user_ids(_env_optional("MODMAIL_BOT_USER_IDS")),
    )
