"""When nothing shows up in the audit log, check whether a moderator just
answered in the channel instead.

Real case: someone pinged the mod role asking whether something was worth an
announcement. The brief correctly recommended no action. A moderator replied
in the channel, directly to the ping, seconds before another moderator
pressed Mark Handled - and the card never mentioned it. There was nothing to
find in the audit log because nothing needed enforcing; the moderator's own
reply *is* the record of what happened, and only a channel reply, not the
audit log, can show it.

Deliberately narrow: only fires when the audit-log scan (the sibling in
test_audit_log_participant_matching.py) found nothing, and only counts a
message that is an actual Discord reply to the brief's own ping or evidence
- not merely nearby in time, which in a busy channel is close to noise.
"""
from datetime import datetime, timezone

import discord

from incident_mod_bot.discord_ui.incident_view import IncidentView

from test_action_summary_delivery import payload

MOD_ROLE_ID = 174997701513969665


class FakeRole:
    def __init__(self, id_):
        self.id = id_


class FakePermissions:
    administrator = False
    moderate_members = False


class FakeModAuthor:
    """A message author with the mod role - what channel.history() actually
    hands back for a guild message, per discord.py's own Member resolution."""

    def __init__(self, name, *, is_mod=True, is_bot=False, id_=266697128234057728):
        self.id = id_
        self.name = name
        self.display_name = name
        self.bot = is_bot
        self.roles = [FakeRole(MOD_ROLE_ID)] if is_mod else []
        self.guild_permissions = FakePermissions()


class FakeReference:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeChannelMessage:
    def __init__(self, id_, author, content, *, reference=None):
        self.id = id_
        self.author = author
        self.clean_content = content
        self.reference = reference
        self.created_at = datetime.now(timezone.utc)


class FakeChannel:
    def __init__(self, messages):
        self._messages = messages

    def history(self, **_kw):
        messages = self._messages

        async def gen():
            for m in messages:
                yield m

        return gen()


class FakeClient:
    def __init__(self, channel):
        self._channel = channel

    async def fetch_channel(self, _id):
        return self._channel


class FakeGuild:
    """Empty audit log - the scenario that makes this path matter."""

    def audit_logs(self, **_kw):
        async def gen():
            return
            yield  # pragma: no cover - makes this an async generator

        return gen()


class FakeInteraction:
    def __init__(self, guild, client):
        self.guild = guild
        self.client = client


def _view(reply_target_message_id):
    return IncidentView(
        payload(
            mod_role_id=MOD_ROLE_ID,
            source_channel_id=175005585203396622,
            reply_targets=[{"user_id": 1, "message_id": reply_target_message_id}],
        ),
        memory_store=None,
        view_store=None,
    )


async def test_a_moderators_reply_to_the_ping_is_the_action_summary():
    anchor_id = 1542361005378764881
    reply = FakeChannelMessage(
        1542361152871338154,
        FakeModAuthor("bidoblob"),
        "honestly I don't know",
        reference=FakeReference(anchor_id),
    )
    view = _view(anchor_id)
    interaction = FakeInteraction(FakeGuild(), FakeClient(FakeChannel([reply])))

    found = await view._collect_recent_mod_actions(interaction)
    assert found == [], "the audit log should have nothing to find in this scenario"

    found = await view._collect_recent_mod_channel_replies(interaction)

    assert found, "the moderator's reply to the ping was not picked up"
    assert "bidoblob" in found[0]
    assert "honestly I don't know" in found[0]


async def test_a_follow_up_from_the_same_moderator_also_counts():
    """The real second half of the same incident: bidoblob's actual answer
    was a reply, but the more substantial message - asking whether to
    forward a GGG member's message - was a plain follow-up two minutes
    later, no reply reference at all. Missing this was the whole complaint:
    a moderator clearing the reply bar makes their next messages part of
    the incident's own conversation, not just channel noise."""
    anchor_id = 1542361005378764881
    reply = FakeChannelMessage(
        1542361152871338154,
        FakeModAuthor("bidoblob"),
        "honestly I don't know",
        reference=FakeReference(anchor_id),
    )
    follow_up = FakeChannelMessage(
        1542361641478258728,
        FakeModAuthor("bidoblob"),
        "do you guys think I should forward the GGG member's message?",
    )
    view = _view(anchor_id)
    interaction = FakeInteraction(FakeGuild(), FakeClient(FakeChannel([reply, follow_up])))

    found = await view._collect_recent_mod_channel_replies(interaction)

    assert len(found) == 2, "the follow-up should ride along with the reply that earned it"
    assert "honestly I don't know" in found[0], "the reply should lead, the follow-up second"
    assert "forward the GGG member's message" in found[1]
    assert "bidoblob" in found[1]


async def test_a_follow_up_from_an_uninvolved_moderator_does_not_count():
    """Clearing the reply bar has to be per moderator, not global - a
    different moderator chatting nearby is exactly the noise case this is
    meant to exclude."""
    anchor_id = 1542361005378764881
    reply = FakeChannelMessage(
        1,
        FakeModAuthor("bidoblob"),
        "honestly I don't know",
        reference=FakeReference(anchor_id),
    )
    unrelated = FakeChannelMessage(
        2,
        FakeModAuthor("Talisen", id_=999999999999999999),
        "unrelated chat, no reference, different mod",
    )
    view = _view(anchor_id)
    interaction = FakeInteraction(FakeGuild(), FakeClient(FakeChannel([reply, unrelated])))

    found = await view._collect_recent_mod_channel_replies(interaction)

    assert len(found) == 1
    assert "bidoblob" in found[0]


async def test_a_reply_from_a_non_moderator_does_not_count():
    anchor_id = 1542361005378764881
    reply = FakeChannelMessage(
        2,
        FakeModAuthor("randomuser", is_mod=False),
        "same, no clue",
        reference=FakeReference(anchor_id),
    )
    view = _view(anchor_id)
    interaction = FakeInteraction(FakeGuild(), FakeClient(FakeChannel([reply])))

    found = await view._collect_recent_mod_channel_replies(interaction)

    assert found == []


async def test_a_moderator_message_that_is_not_a_reply_does_not_count():
    """Being in the channel afterward isn't the same as answering - this is
    a very busy channel and moderators are in it constantly for other reasons."""
    anchor_id = 1542361005378764881
    unrelated = FakeChannelMessage(
        3, FakeModAuthor("bidoblob"), "unrelated chat, no reference at all"
    )
    view = _view(anchor_id)
    interaction = FakeInteraction(FakeGuild(), FakeClient(FakeChannel([unrelated])))

    found = await view._collect_recent_mod_channel_replies(interaction)

    assert found == []


async def test_a_bot_reply_does_not_count():
    anchor_id = 1542361005378764881
    reply = FakeChannelMessage(
        4,
        FakeModAuthor("Mod", is_bot=True),
        "some automated note",
        reference=FakeReference(anchor_id),
    )
    view = _view(anchor_id)
    interaction = FakeInteraction(FakeGuild(), FakeClient(FakeChannel([reply])))

    found = await view._collect_recent_mod_channel_replies(interaction)

    assert found == []
