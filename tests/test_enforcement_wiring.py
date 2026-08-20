"""Button presses must reach the ledger, carrying what the brief recommended.

The payload previously carried no recommendations/rule_refs, so there was
nothing to compare a moderator's action against.
"""
import types

import pytest
import pytest_asyncio

from incident_mod_bot.discord_ui.incident_view import IncidentView, IncidentViewPayload
from incident_mod_bot.memory.store import MemoryStore

GUILD = 174993814845521922


def make_payload(**kw):
    base = dict(
        draft_message="please stop",
        memory_suggestions={},
        mod_role_id=None,
        participants=[],
        evidence_quotes=[],
        recommendations=["Delete the message", "Warn about RMT"],
        rule_ids=["spam"],
    )
    base.update(kw)
    return IncidentViewPayload(**base)


def fake_interaction(guild_id=GUILD, user_id=42, message_id=7):
    return types.SimpleNamespace(
        guild_id=guild_id,
        user=types.SimpleNamespace(id=user_id),
        message=types.SimpleNamespace(id=message_id),
    )


@pytest_asyncio.fixture
async def store(tmp_path):
    s = MemoryStore(str(tmp_path / "t.sqlite3"))
    await s.connect()
    yield s
    await s.close()


def test_payload_carries_what_the_bot_recommended():
    p = make_payload()
    assert p.recommendations == ["Delete the message", "Warn about RMT"]
    assert p.rule_ids == ["spam"]
    assert p.to_dict()["recommendations"] == ["Delete the message", "Warn about RMT"]


def test_payload_version_bumped_so_old_views_are_not_misread():
    assert make_payload().view_version >= 3


async def test_logging_an_action_records_the_recommendation_alongside_it(store):
    view = IncidentView(make_payload(), store, view_store=None)

    await view._log_enforcement(fake_interaction(), "deleted", target_user_ids=[555])

    rows = await store.list_enforcement_entries(GUILD)
    assert len(rows) == 1
    assert rows[0]["action"] == "deleted"
    assert rows[0]["mod_user_id"] == 42
    assert rows[0]["recommended"] == ["Delete the message", "Warn about RMT"]
    assert rows[0]["rule_ids"] == ["spam"]
    assert rows[0]["target_user_ids"] == [555]


async def test_logging_never_raises_into_the_button_handler(store):
    """A ledger failure must never break moderation."""
    view = IncidentView(make_payload(), store, view_store=None)
    broken = types.SimpleNamespace(guild_id=None, user=None, message=None)

    await view._log_enforcement(broken, "deleted")  # must not raise


async def test_a_failing_store_is_swallowed_not_raised(store):
    """Exercises the except branch itself - an early return would not reach it."""
    class Exploding:
        async def add_enforcement_entry(self, **kw):
            raise RuntimeError("db is gone")

    view = IncidentView(make_payload(), Exploding(), view_store=None)

    await view._log_enforcement(fake_interaction(), "deleted")  # must not raise
