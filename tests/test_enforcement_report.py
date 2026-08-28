"""Make the ledger's payoff visible.

Memory died the first time because pressing the button produced nothing anyone
could see. The ledger is walking into the same trap: it fills silently. These
cover the two things that close the loop - an /enforcement view, and a line on
briefs saying the history was actually used.
"""
import pytest
import pytest_asyncio

from incident_mod_bot.memory.store import MemoryStore, format_enforcement_report

GUILD = 174993814845521922


@pytest_asyncio.fixture
async def store(tmp_path):
    s = MemoryStore(str(tmp_path / "t.sqlite3"))
    await s.connect()
    yield s
    await s.close()


async def seed(store, n_delete=3, n_other=1):
    for _ in range(n_delete):
        await store.add_enforcement_entry(
            guild_id=GUILD, channel_id=1, brief_message_id=2, mod_user_id=99,
            action="deleted", target_user_ids=[555], target_message_ids=[7],
            rule_ids=["spam"], recommended=["Delete the message"], outcome=None)
    for _ in range(n_other):
        await store.add_enforcement_entry(
            guild_id=GUILD, channel_id=1, brief_message_id=2, mod_user_id=42,
            action="action_taken", target_user_ids=[556], target_message_ids=[],
            rule_ids=["spam"], recommended=["Timeout the user"], outcome=None)


# --- stats -----------------------------------------------------------------

async def test_stats_count_actions_and_moderators(store):
    await seed(store)

    stats = await store.enforcement_stats(GUILD)

    assert stats["total"] == 4
    assert stats["by_action"]["deleted"] == 3
    assert stats["moderators"] == 2


async def test_stats_are_empty_before_anything_happens(store):
    assert (await store.enforcement_stats(GUILD))["total"] == 0


# --- the report ------------------------------------------------------------

def test_report_says_so_plainly_when_there_is_no_history():
    text = format_enforcement_report(norms=[], divergence=[], stats={"total": 0, "by_action": {}, "moderators": 0})
    assert "no enforcement history" in text.lower()


def test_report_shows_norms_and_divergence():
    text = format_enforcement_report(
        norms=["spam: usually deleted (3 of 4)."],
        divergence=["Recommended 'timeout' 3 time(s) where the moderator did something else."],
        stats={"total": 4, "by_action": {"deleted": 3, "action_taken": 1}, "moderators": 2},
    )
    assert "spam: usually deleted" in text
    assert "timeout" in text
    assert "4" in text


def test_report_never_leaks_user_ids():
    text = format_enforcement_report(
        norms=["spam: usually deleted (3 of 4)."], divergence=[],
        stats={"total": 4, "by_action": {"deleted": 3}, "moderators": 1},
    )
    assert "555" not in text and "556" not in text


# --- enforcement history informs the brief, but never says so on the card --

@pytest.fixture
def bot():
    from incident_mod_bot.bot import IncidentBot
    return IncidentBot.__new__(IncidentBot)


def minimal_result():
    from incident_mod_bot.pipeline.incident import IncidentResult, MemorySuggestions
    return IncidentResult(
        headline="RMT link - delete + warn", summary="s", draft_message="d",
        confidence=0.9, memory_suggestions=MemorySuggestions(),
    )


def test_the_card_never_mentions_enforcement_history(bot):
    """informed_by is tracked internally (see enforcement_stats) but was never
    something a moderator could act on as a bare count, so it doesn't render."""
    result = minimal_result()
    result.informed_by = 3
    embed = bot._build_incident_embed(result)
    assert "informed by" not in (embed.description or "").lower()
