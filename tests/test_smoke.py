from __future__ import annotations

import pytest

from incident_mod_bot.memory.store import MemoryStore
from incident_mod_bot.pipeline.incident import parse_incident_result
from incident_mod_bot.utils.text import compress_text, truncate


def test_text_helpers() -> None:
    assert truncate("hello", 10) == "hello"
    assert truncate("hello world", 6) == "hello…"
    assert compress_text("  a\n\n b  ") == "a b"


def test_incident_result_parsing_minimal() -> None:
    result = parse_incident_result({"summary": "x", "draft_message": "y"})
    assert result.summary == "x"
    assert result.draft_message == "y"


@pytest.mark.anyio
async def test_memory_store_schema_and_roundtrip(tmp_path) -> None:
    db_path = str(tmp_path / "test.sqlite3")
    store = MemoryStore(db_path)
    await store.connect()
    await store.set_guild_config(123, rules_channel_id=456, mod_role_id=789)
    cfg = await store.get_guild_config(123)
    assert cfg["rules_channel_id"] == 456
    assert cfg["mod_role_id"] == 789
    await store.set_rules_memory(123, "{\"rules\": []}")
    assert await store.get_rules_memory(123) == "{\"rules\": []}"
    await store.add_user_observation(123, 1, "gets heated", "https://example.com")
    await store.add_user_observation(123, 1, "gets heated", "https://example.com")
    entries = await store.list_user_profile_entries(123, 1)
    assert entries[0][0] == "gets heated"
    assert entries[0][1] >= 2
    await store.close()
