"""The enforcement ledger: record what mods actually did, not what the model guessed.

Replaces speculative per-user memory (0 rows in months, structurally dead) with
ground truth captured automatically from button presses. Derived memory is
aggregate — rules, enforcement norms, trends — never per-user profiles.
Target user ids ARE stored for audit/repeat lookups, but are not fed to the model.
"""
import time

import pytest
import pytest_asyncio

from incident_mod_bot.memory.store import MemoryStore

GUILD = 174993814845521922


@pytest_asyncio.fixture
async def store(tmp_path):
    s = MemoryStore(str(tmp_path / "t.sqlite3"))
    await s.connect()
    yield s
    await s.close()


async def test_a_button_press_is_recorded(store):
    await store.add_enforcement_entry(
        guild_id=GUILD, channel_id=1, brief_message_id=2, mod_user_id=99,
        action="deleted", target_user_ids=[555], target_message_ids=[777],
        rule_ids=["spam"], recommended=["Delete the message", "Warn about RMT"],
        outcome=None,
    )

    rows = await store.list_enforcement_entries(GUILD, limit=10)

    assert len(rows) == 1
    assert rows[0]["action"] == "deleted"
    assert rows[0]["mod_user_id"] == 99


async def test_target_users_are_stored_for_audit(store):
    """Kept deliberately: audit trail and repeat lookups. Not fed to the model."""
    await store.add_enforcement_entry(
        guild_id=GUILD, channel_id=1, brief_message_id=2, mod_user_id=99,
        action="timeout", target_user_ids=[555, 556], target_message_ids=[],
        rule_ids=[], recommended=[], outcome="10m",
    )

    rows = await store.list_enforcement_entries(GUILD, limit=10)

    assert rows[0]["target_user_ids"] == [555, 556]
    assert rows[0]["outcome"] == "10m"


async def test_entries_are_scoped_per_guild(store):
    await store.add_enforcement_entry(
        guild_id=GUILD, channel_id=1, brief_message_id=2, mod_user_id=99,
        action="action_taken", target_user_ids=[], target_message_ids=[],
        rule_ids=[], recommended=[], outcome=None,
    )
    await store.add_enforcement_entry(
        guild_id=999, channel_id=1, brief_message_id=3, mod_user_id=99,
        action="action_taken", target_user_ids=[], target_message_ids=[],
        rule_ids=[], recommended=[], outcome=None,
    )

    assert len(await store.list_enforcement_entries(GUILD, limit=10)) == 1


async def test_enforcement_norms_are_aggregate_not_per_user(store):
    """The derived memory the model sees must never name individuals."""
    for _ in range(3):
        await store.add_enforcement_entry(
            guild_id=GUILD, channel_id=1, brief_message_id=2, mod_user_id=99,
            action="deleted", target_user_ids=[555], target_message_ids=[],
            rule_ids=["spam"], recommended=["Delete"], outcome=None,
        )
    await store.add_enforcement_entry(
        guild_id=GUILD, channel_id=1, brief_message_id=2, mod_user_id=99,
        action="timeout", target_user_ids=[556], target_message_ids=[],
        rule_ids=["spam"], recommended=["Delete"], outcome=None,
    )

    norms = await store.summarize_enforcement(GUILD)

    blob = " ".join(norms).lower()
    assert "spam" in blob
    assert "deleted" in blob
    assert "555" not in blob and "556" not in blob, "norms must not leak user ids"


async def test_norms_surface_recommended_vs_actual_divergence(store):
    """The signal that tells us whether the bot's advice is any good."""
    for _ in range(4):
        await store.add_enforcement_entry(
            guild_id=GUILD, channel_id=1, brief_message_id=2, mod_user_id=99,
            action="action_taken", target_user_ids=[1], target_message_ids=[],
            rule_ids=["spam"], recommended=["Timeout the user"], outcome=None,
        )

    divergence = await store.enforcement_divergence(GUILD)

    assert divergence, "should report when advice and action disagree"
    assert any("timeout" in d.lower() for d in divergence)


async def test_empty_history_yields_no_norms(store):
    assert await store.summarize_enforcement(GUILD) == []
