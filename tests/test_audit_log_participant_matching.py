"""The matching half of the action summary: audit log entries against the
people named in the brief.

payload.participants is a list of plain dicts (see the participant select in
IncidentView.__init__), not the pydantic Participant objects analyze_incident
returns. Attribute access on that list quietly matched nothing -
getattr(a_dict, "user_id", None) returns the default instead of raising - so
participant_ids came back empty unless a moderator had used the participant
dropdown themselves, and a real ban against the brief's own offender was
never found.
"""
from datetime import datetime, timezone

import discord

from incident_mod_bot.discord_ui.incident_view import IncidentView

from test_action_summary_delivery import payload


class FakeUser:
    def __init__(self, id_, name):
        self.id = id_
        self.name = name
        self.display_name = name


class FakeTarget:
    def __init__(self, id_):
        self.id = id_


class FakeEntry:
    def __init__(self, action, *, target_id, user_id, user_name="Sable"):
        self.action = action
        self.target = FakeTarget(target_id)
        self.user = FakeUser(user_id, user_name)
        self.created_at = datetime.now(timezone.utc)
        self.extra = None
        self.after = None


class FakeGuild:
    def __init__(self, entries):
        self._entries = entries

    def audit_logs(self, **_kw):
        entries = self._entries

        async def gen():
            for e in entries:
                yield e

        return gen()


class FakeInteraction:
    def __init__(self, guild):
        self.guild = guild


async def test_dict_shaped_participants_are_actually_matched():
    """The real incident: spamuser1's ban never reached the card."""
    view = IncidentView(
        payload(participants=[{"user_id": 42001, "name": "spamuser1"}]),
        memory_store=None,
        view_store=None,
    )
    ban = FakeEntry(
        discord.AuditLogAction.ban,
        target_id=42001,
        user_id=1,
        user_name="Talisen",
    )
    interaction = FakeInteraction(FakeGuild([ban]))

    found = await view._collect_recent_mod_actions(interaction)

    assert found, "a ban against a brief's own participant was not found"
    assert "Talisen" in found[0]


async def test_a_participant_missing_a_user_id_is_skipped_not_fatal():
    """Malformed payload entries must not crash the lookup."""
    view = IncidentView(
        payload(participants=[{"name": "no id here"}, {"user_id": "not-an-int"}]),
        memory_store=None,
        view_store=None,
    )
    interaction = FakeInteraction(FakeGuild([]))

    found = await view._collect_recent_mod_actions(interaction)

    assert found == []
