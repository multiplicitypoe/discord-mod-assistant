"""The action summary has to actually arrive on the card.

It never once did. The audit log held the answer, the matcher recognised it,
and the summary still never appeared, because it ran as a task whose only
reference was a local variable. The event loop keeps weak references to tasks,
so it was collected part way through and cancelled in silence.

These cover the delivery path rather than the matching, which the sibling file
already covers: that the task is held until it finishes, that a moderator who
acts a few minutes after pressing the button is still noticed, and that finding
nothing leaves the card alone and says so in the log.
"""
import asyncio
import gc
import logging

import discord

from incident_mod_bot.discord_ui import incident_view as iv
from incident_mod_bot.discord_ui.incident_view import IncidentView, IncidentViewPayload


def payload(**kw):
    base = dict(
        draft_message="please stop",
        memory_suggestions={},
        mod_role_id=None,
        participants=[{"user_id": 1, "name": "testuser1"}],
        evidence_quotes=[{"message_id": 2, "quote": "x"}],
        reply_targets=[{"user_id": 1}],
        recommendations=["Delete"],
        rule_ids=["spam"],
    )
    base.update(kw)
    return IncidentViewPayload(**base)


class FakeMessage:
    """Just enough of a Discord message to be edited."""

    def __init__(self, embed):
        self.id = 999
        self.embeds = [embed] if embed else []
        self.edits = []
        self.channel = self

    async def fetch_message(self, _id):
        return self

    async def edit(self, *, embed=None, view=None):
        self.edits.append(embed)
        self.embeds = [embed] if embed else []


def brief_message():
    embed = discord.Embed(title="Possible RMT solicitation", description="body")
    return FakeMessage(embed)


def fields_named(embed, name):
    return [f for f in embed.fields if f.name == name]


async def test_a_background_task_is_held_until_it_finishes():
    """The bug itself. A task nobody holds can be collected mid flight."""
    finished = []

    async def work():
        await asyncio.sleep(0.01)
        finished.append(True)

    task = iv._spawn(work(), "test")
    assert task in iv._BACKGROUND_TASKS
    del task
    gc.collect()
    await asyncio.sleep(0.05)
    assert finished == [True], "the task was collected before it could finish"
    assert not iv._BACKGROUND_TASKS, "finished tasks should be let go of"


async def test_a_cancelled_background_task_says_so(caplog):
    caplog.set_level(logging.WARNING, logger="incident_mod_bot")

    async def work():
        await asyncio.sleep(5)

    task = iv._spawn(work(), "test summary")
    task.cancel()
    await asyncio.sleep(0.01)
    assert any("cancel" in r.message.lower() for r in caplog.records)


async def test_a_moderator_who_acts_after_pressing_is_still_noticed(monkeypatch):
    """The forward gap. Pressing the button and then going to ban someone is
    the normal order, so one look at the moment of the press is too early."""
    monkeypatch.setattr(iv, "_AUDIT_FOLLOW_UP_S", (0, 0, 0))
    view = IncidentView(payload(), memory_store=None, view_store=None)
    calls = []

    async def collect(_interaction, since=None):
        calls.append(since)
        if len(calls) < 3:
            return []
        return ["Banned testuser1 · by Sable"]

    monkeypatch.setattr(view, "_collect_recent_mod_actions", collect)
    message = brief_message()
    await view._attach_action_summary(object(), message)

    assert len(calls) >= 3, "it gave up before the moderator had acted"
    shown = fields_named(message.embeds[0], "Action taken")
    assert shown, "the late ban never reached the card"
    assert "Banned testuser1" in shown[0].value


async def test_the_window_keeps_reaching_back_as_the_looks_go_on(monkeypatch):
    """Later looks must not slide the window forward off the earlier action."""
    monkeypatch.setattr(iv, "_AUDIT_FOLLOW_UP_S", (0, 0))
    view = IncidentView(payload(), memory_store=None, view_store=None)
    seen = []

    async def collect(_interaction, since=None):
        seen.append(since)
        return []

    monkeypatch.setattr(view, "_collect_recent_mod_actions", collect)
    await view._attach_action_summary(object(), brief_message())
    assert len(set(seen)) == 1, "the start of the window moved between looks"
    assert seen[0] is not None


async def test_finding_nothing_leaves_the_card_alone_and_says_so(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="incident_mod_bot")
    monkeypatch.setattr(iv, "_AUDIT_FOLLOW_UP_S", (0,))
    view = IncidentView(payload(), memory_store=None, view_store=None)

    async def collect(_interaction, since=None):
        return []

    monkeypatch.setattr(view, "_collect_recent_mod_actions", collect)
    message = brief_message()
    await view._attach_action_summary(object(), message)

    assert message.edits == [], "an empty result should not touch the card"
    assert not fields_named(message.embeds[0], "Action taken")
    assert any("audit" in r.message.lower() for r in caplog.records), \
        "having looked and found nothing should still be recorded"


async def test_a_later_find_replaces_the_field_rather_than_stacking_a_second(monkeypatch):
    monkeypatch.setattr(iv, "_AUDIT_FOLLOW_UP_S", (0,))
    view = IncidentView(payload(), memory_store=None, view_store=None)
    results = [
        ["Deleted a message from testuser1 · by Sable"],
        ["Deleted a message from testuser1 · by Sable",
         "Banned testuser1 · by Sable"],
    ]

    async def collect(_interaction, since=None):
        return results.pop(0) if results else []

    monkeypatch.setattr(view, "_collect_recent_mod_actions", collect)
    message = brief_message()
    await view._attach_action_summary(object(), message)

    shown = fields_named(message.embeds[0], "Action taken")
    assert len(shown) == 1, "the card grew a second copy of the field"
    assert "Banned" in shown[0].value and "Deleted" in shown[0].value
