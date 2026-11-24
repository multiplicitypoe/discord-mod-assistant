"""The same ban must read the same on every look.

A banned account is no longer cached by the time later looks run, so what
discord.py called it varied: the first look said "Banned user_p", a rebuilt
card's own look said "Banned Pip" (the account's display name). The two lines
differ, merge_new_lines treats different lines as different events, and the
card showed one ban as two. People in the analysed window are now named the way
the brief's own analysis named them.
"""
import discord

from incident_mod_bot.discord_ui.incident_view import IncidentView, merge_new_lines

from test_action_summary_delivery import payload
from test_audit_log_participant_matching import FakeEntry, FakeGuild, FakeTarget
from test_audit_window_authors import ANCHOR_ID, MsgInteraction

BANNED = 2002


class Named(FakeTarget):
    def __init__(self, id_, name, display_name):
        super().__init__(id_)
        self.name = name
        self.display_name = display_name


class Store:
    async def get_incident_payload(self, brief_id):
        return (1, {"messages": [{"author_id": 2001, "author_name": "reporter_a", "content": "x"},
                                 {"author_id": BANNED, "author_name": "user_p", "content": ""}],
                    "authors": {"2001": {}, str(BANNED): {}}})


def view():
    return IncidentView(
        payload(participants=[{"user_id": 1, "name": "reporter_a"}], anchor_message_id=ANCHOR_ID),
        memory_store=Store(), view_store=None,
    )


def ban(display_name):
    entry = FakeEntry(discord.AuditLogAction.ban, target_id=BANNED, user_id=9, user_name="mod_a")
    entry.target = Named(BANNED, "user_p", display_name)
    return entry


async def test_a_banned_window_author_keeps_the_brief_s_name_whatever_discord_calls_them_now():
    first = await view()._collect_recent_mod_actions(MsgInteraction(FakeGuild([ban("user_p")])))
    later = await view()._collect_recent_mod_actions(MsgInteraction(FakeGuild([ban("Pip")])))

    assert first == later == ["Banned user_p · by mod_a"]
    merged, changed = merge_new_lines(first, later)
    assert merged == first and not changed


async def test_without_a_saved_payload_the_audit_name_is_still_used():
    class Empty:
        async def get_incident_payload(self, brief_id):
            return None

    v = IncidentView(payload(participants=[{"user_id": BANNED, "name": "from_brief"}], anchor_message_id=ANCHOR_ID),
                     memory_store=Empty(), view_store=None)
    found = await v._collect_recent_mod_actions(MsgInteraction(FakeGuild([ban("Pip")])))
    assert found == ["Banned from_brief · by mod_a"]
