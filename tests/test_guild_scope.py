from __future__ import annotations

import os
from unittest import mock

from incident_mod_bot.config import DEFAULT_ACTIVE_GUILD_IDS, load_settings

POE = 174993814845521922
CODE_LYOKO = 604756656378871848


def _settings(**env: str):
    base = {"DISCORD_TOKEN": "t", "OPENAI_API_KEY": "k"}
    base.update(env)
    with mock.patch.dict(os.environ, base, clear=True):
        return load_settings()


def test_defaults_to_the_poe_guild_only() -> None:
    settings = _settings()
    assert settings.active_guild_ids == DEFAULT_ACTIVE_GUILD_IDS
    assert settings.is_active_guild(POE)


def test_other_servers_are_inert_by_default() -> None:
    """The bot is invited to servers it must only read, so anything not named
    here gets no reactions, no analysis and no commands."""
    settings = _settings()
    assert not settings.is_active_guild(CODE_LYOKO)


def test_a_message_with_no_guild_is_not_active() -> None:
    assert not _settings().is_active_guild(None)


def test_env_var_overrides_and_tolerates_spacing() -> None:
    settings = _settings(ACTIVE_GUILD_IDS=" 111, 222 ,")
    assert settings.active_guild_ids == frozenset({111, 222})
    assert settings.is_active_guild(111)
    assert not settings.is_active_guild(POE)


def test_empty_value_means_no_guild_is_active() -> None:
    """Fail closed. An empty setting must not be read as 'everywhere'."""
    settings = _settings(ACTIVE_GUILD_IDS="")
    assert settings.active_guild_ids == frozenset()
    assert not settings.is_active_guild(POE)


def test_modmail_bot_user_ids_default_to_empty() -> None:
    """Unset means the feature is off, not 'watch every bot'."""
    assert _settings().modmail_bot_user_ids == frozenset()


def test_modmail_bot_user_ids_parses_like_guild_ids() -> None:
    settings = _settings(MODMAIL_BOT_USER_IDS=" 590765760092307456, 111 ,")
    assert settings.modmail_bot_user_ids == frozenset({590765760092307456, 111})
