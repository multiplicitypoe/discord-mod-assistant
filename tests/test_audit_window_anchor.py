"""The lookback window has to be anchored to the incident, not to whenever
Mark Handled happens to get pressed.

Real case: a brief handled ~90 seconds after its own ping still surfaced an
unrelated timeout from 1h25m *before the ping even happened*, because the
old window was "now (the press) minus 2 hours" - fast presses reached
backward past the incident's own start, slow presses reached forward past
it. Anchoring to the ping's own timestamp and shrinking the window fixes
both directions at once.
"""
from datetime import datetime, timedelta, timezone

import discord

from incident_mod_bot.discord_ui import incident_view as iv
from incident_mod_bot.discord_ui.incident_view import IncidentView

from test_action_summary_delivery import payload
from test_audit_log_participant_matching import FakeInteraction, FakeTarget, FakeUser

# A real snowflake with a known creation time, so the expected window start
# can be computed independently of whatever "now" happens to be.
ANCHOR_ID = 1542361005378764881


class FilteringFakeGuild:
    """Unlike the sibling fakes, this one actually honours `after=` - the
    whole point here is proving what does and doesn't make it through the
    window, which a fake that ignores its arguments can't demonstrate."""

    def __init__(self, entries):
        self._entries = entries

    def audit_logs(self, *, after=None, **_kw):
        entries = self._entries

        async def gen():
            for e in entries:
                if after is not None and e.created_at <= after:
                    continue
                yield e

        return gen()


class FakeEntry:
    def __init__(self, action, *, target_id, user_id, created_at, user_name="Sable"):
        self.action = action
        self.target = FakeTarget(target_id)
        self.user = FakeUser(user_id, user_name)
        self.created_at = created_at
        self.extra = None
        self.after = None


async def test_the_window_start_is_computed_from_the_anchor_not_from_now():
    view = IncidentView(
        payload(anchor_message_id=ANCHOR_ID), memory_store=None, view_store=None
    )
    expected = iv._snowflake_created_at(ANCHOR_ID) - timedelta(
        seconds=iv._AUDIT_LOOKBACK_BEFORE_S
    )
    assert view._audit_window_start() == expected


async def test_the_window_falls_back_to_now_with_no_anchor():
    """A manually-run /mod scan has no single triggering message."""
    view = IncidentView(payload(), memory_store=None, view_store=None)
    start = view._audit_window_start()
    expected = datetime.now(timezone.utc) - timedelta(seconds=iv._AUDIT_LOOKBACK_BEFORE_S)
    assert abs((start - expected).total_seconds()) < 5


async def test_moderation_from_well_before_the_ping_is_excluded():
    """The actual bug: a real ban from 1h25m before the ping showed up on a
    brief that had nothing to do with it."""
    view = IncidentView(
        payload(
            anchor_message_id=ANCHOR_ID,
            participants=[{"user_id": 99, "name": "unrelated_user"}],
        ),
        memory_store=None,
        view_store=None,
    )
    ping_time = iv._snowflake_created_at(ANCHOR_ID)
    stale_ban = FakeEntry(
        discord.AuditLogAction.ban,
        target_id=99,
        user_id=1,
        created_at=ping_time - timedelta(hours=1, minutes=25),
    )
    interaction = FakeInteraction(FilteringFakeGuild([stale_ban]))

    found = await view._collect_recent_mod_actions(interaction)

    assert found == [], "moderation from well before this incident should not attach to it"


async def test_moderation_from_just_before_the_ping_is_still_included():
    """The window still reaches back a little - a mod who acted moments
    before the ping (and the report is really about that) shouldn't vanish."""
    view = IncidentView(
        payload(
            anchor_message_id=ANCHOR_ID,
            participants=[{"user_id": 99, "name": "unrelated_user"}],
        ),
        memory_store=None,
        view_store=None,
    )
    ping_time = iv._snowflake_created_at(ANCHOR_ID)
    recent_ban = FakeEntry(
        discord.AuditLogAction.ban,
        target_id=99,
        user_id=1,
        created_at=ping_time - timedelta(minutes=2),
    )
    interaction = FakeInteraction(FilteringFakeGuild([recent_ban]))

    found = await view._collect_recent_mod_actions(interaction)

    assert found, "moderation from just before the ping should still be picked up"
