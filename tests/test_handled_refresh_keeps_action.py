"""A Mark Handled follow-up must not wipe the timeout it just found.

A real brief: the moderator pressed Mark Handled, then timed the offender out a
couple of minutes later. The follow-up look found the timeout and put it on the
card, then asked for an evidence rebuild. The rebuild starts from a fresh
embed, ran its own audit look (which could not see the offender - they were
not a named participant and the stand-in it passed carried no brief id, so the
set of people who spoke in the window came back empty), and so replaced the
card with no Action-taken field and re-ran the analysis as though nobody had
acted. The card went from "Transphobic remark" to a hedged verdict with no
trace of the timeout, and nothing later put it back.

Two halves, each covered: the rebuild's own look now recognises the people who
spoke in the window, and whatever the follow-up already showed is handed to the
rebuild rather than left for it to rediscover.
"""
from datetime import timedelta
from types import SimpleNamespace

import discord

from incident_mod_bot.bot import IncidentBot
from incident_mod_bot.discord_ui.incident_view import IncidentView
from incident_mod_bot.pipeline.incident import IncidentResult

from test_action_summary_delivery import payload
from test_audit_log_participant_matching import FakeEntry, FakeGuild

ANCHOR_ID = 1_000_000_000_000_000_000
BRIEF_ID = 777
OFFENDER = 2002

SAVED = {
    "messages": [{"id": "1", "author_id": 2001, "content": "x"}, {"id": "2", "author_id": OFFENDER, "content": "y"}],
    "authors": {"2001": {}, str(OFFENDER): {}},
    "audit_findings": [],
}


class FakeStore:
    def __init__(self):
        self.saved = dict(SAVED)
        self.writes = []

    async def get_incident_payload(self, brief_id):
        return (1, self.saved)

    async def save_incident_payload(self, brief_id, guild_id, data):
        self.saved = data
        self.writes.append(data)


class FakeViewStore:
    async def load_view(self, message_id):
        return None

    async def save_view(self, record):
        pass


class Channel:
    id = 55

    def history(self, **_kw):
        async def gen():
            return
            yield  # pragma: no cover

        return gen()


class Message:
    def __init__(self):
        self.id = BRIEF_ID
        self.embeds = []
        self.edits = []
        self.channel = Channel()
        self.guild = SimpleNamespace(id=1)

    async def edit(self, *, embed=None, view=None):
        self.edits.append(embed)


def timeout_entry():
    entry = FakeEntry(discord.AuditLogAction.member_update, target_id=OFFENDER, user_id=9, user_name="mod_a")
    entry.after = SimpleNamespace(timed_out_until=entry.created_at + timedelta(days=7))
    entry.reason = "transphobia"
    return entry


def build(entries):
    store = FakeStore()
    view = IncidentView(
        payload(participants=[{"user_id": 1, "name": "reporter_a"}], anchor_message_id=ANCHOR_ID, source_channel_id=55),
        memory_store=store,
        view_store=FakeViewStore(),
    )
    bot = IncidentBot.__new__(IncidentBot)
    bot.settings = SimpleNamespace(debug_logs=False)
    bot.memory_store = store
    bot.view_store = FakeViewStore()
    asked = {}

    async def analyze(**kw):
        asked.update(kw)
        result = IncidentResult(summary="s", draft_message="d", headline="Transphobic remark in chat")
        return result, {}, {"messages": SAVED["messages"], "authors": SAVED["authors"]}

    bot._analyze_incident_messages = analyze
    bot._compress_messages = lambda msgs, seen: []
    return bot, view, store, asked, FakeGuild(entries)


async def refresh(bot, view, guild, **kw):
    message = Message()
    anchor = SimpleNamespace(
        id=ANCHOR_ID,
        created_at=timeout_entry().created_at,
        raw_role_mentions=[],
        reference=None,
    )
    await bot._refresh_incident_evidence(
        message=message,
        view=view,
        anchor=anchor,
        channel=message.channel,
        guild=guild,
        title="Auto Mod Brief",
        context=None,
        mod_role_id=None,
        guild_id=1,
        ctx="test",
        **kw,
    )
    return message


def action_field(message):
    assert message.edits, "the brief was never rebuilt"
    fields = [f for f in message.edits[-1].fields if f.name == "Action taken:"]
    return fields[0].value if fields else None


async def test_rebuild_sees_a_timeout_on_someone_who_only_spoke_in_the_window():
    bot, view, store, asked, guild = build([timeout_entry()])

    message = await refresh(bot, view, guild)

    value = action_field(message)
    assert value and "Timed out" in value and "7d" in value
    assert any("Timed out" in line for line in asked["audit_findings"])
    assert any("Timed out" in line for line in store.saved["audit_findings"])


async def test_rebuild_keeps_what_the_followup_already_showed():
    """Even when the rebuild's own look finds nothing, the card keeps the field."""
    bot, view, store, asked, guild = build([])
    shown = ["Timed out user_q for 7d (transphobia) · by mod_a"]

    message = await refresh(bot, view, guild, audit_lines=shown)

    value = action_field(message)
    assert value and "Timed out user_q for 7d" in value
    assert asked["audit_findings"] == shown
    assert store.saved["audit_findings"] == shown


async def test_no_findings_still_means_no_field():
    bot, view, store, asked, guild = build([])

    message = await refresh(bot, view, guild, force=True)

    assert action_field(message) is None
