"""A moderator bans someone the brief never named - e.g. the reporter said
"likely bot in chat" and the brief could not tell which account. The ban is in
the audit log, but the summary only looked at people the brief named, so the
card never showed it. Anyone who spoke in the analysed window now counts, for
actions after the incident started."""
from datetime import datetime, timedelta, timezone

import discord

from incident_mod_bot.discord_ui.incident_view import IncidentView

from test_action_summary_delivery import payload
from test_audit_log_participant_matching import FakeEntry, FakeGuild, FakeInteraction

ANCHOR_ID = 1_000_000_000_000_000_000  # snowflake created in 2018; fine for ordering


class FakeStore:
    def __init__(self, saved):
        self.saved = saved

    async def get_incident_payload(self, brief_id):
        return (1, self.saved)


class MsgInteraction(FakeInteraction):
    def __init__(self, guild):
        super().__init__(guild)
        self.message = type("M", (), {"id": 777})()


def _view(saved):
    return IncidentView(
        payload(participants=[{"user_id": 1, "name": "reporter_a"}], anchor_message_id=ANCHOR_ID),
        memory_store=FakeStore(saved),
        view_store=None,
    )


SAVED = {
    "messages": [{"author_id": 2001, "content": "x"}, {"author_id": 2002, "content": "y"}],
    "authors": {"2001": {}, "2002": {}},
}


async def test_ban_of_window_author_not_in_brief_is_shown():
    ban = FakeEntry(discord.AuditLogAction.ban, target_id=2002, user_id=9, user_name="mod_a")
    found = await _view(SAVED)._collect_recent_mod_actions(MsgInteraction(FakeGuild([ban])))
    assert found and "Banned" in found[0] and "mod_a" in found[0]


async def test_ban_of_someone_outside_the_window_is_ignored():
    ban = FakeEntry(discord.AuditLogAction.ban, target_id=5555, user_id=9)
    found = await _view(SAVED)._collect_recent_mod_actions(MsgInteraction(FakeGuild([ban])))
    assert found == []


async def test_window_author_action_before_the_incident_is_ignored():
    ban = FakeEntry(discord.AuditLogAction.ban, target_id=2002, user_id=9)
    ban.created_at = datetime(2010, 1, 1, tzinfo=timezone.utc) + timedelta(days=1)
    found = await _view(SAVED)._collect_recent_mod_actions(MsgInteraction(FakeGuild([ban])))
    assert found == []


async def test_missing_saved_payload_changes_nothing():
    class Empty(FakeStore):
        async def get_incident_payload(self, brief_id):
            return None

    view = IncidentView(payload(participants=[]), memory_store=Empty({}), view_store=None)
    ban = FakeEntry(discord.AuditLogAction.ban, target_id=2002, user_id=9)
    assert await view._collect_recent_mod_actions(MsgInteraction(FakeGuild([ban]))) == []
