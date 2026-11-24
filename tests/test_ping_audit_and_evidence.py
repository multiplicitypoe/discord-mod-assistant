"""The two November briefs that said nothing happened, and the evidence
links that kept disappearing.

2026-11-10: a referral link drew "modssss" and "nuke this mf", then a bare
ping. The guess was the build talk right above the ping, and the brief said
"No action" directly over "Banned <spammer>". 2026-11-09: a reply-ping on an
innocent line, while chat said the poster was someone timed out five
minutes earlier. Names are placeholders.
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import discord

from incident_mod_bot.bot import IncidentBot
from incident_mod_bot.discord_ui.incident_view import (
    _snowflake_created_at,
    collect_related_mod_actions,
)
from incident_mod_bot.pipeline.incident import EvidenceQuote, IncidentResult
from incident_mod_bot.pipeline.ping_context import (
    bystander_callouts,
    enforced_names,
    enforcement_conflict,
    reported_message_pointer,
    snowflake_at_ms,
)
from incident_mod_bot.utils import brief_log

T0 = 1_762_750_000_000


def sid(seconds: float, n: int = 0) -> int:
    return snowflake_at_ms(T0 + int(seconds * 1000)) + n


REPORTER, SPAMMER, TALKER, CALLER_A, CALLER_B = 1, 2, 3, 4, 5
NAMES = {REPORTER: "reporter_k", SPAMMER: "spam_user_d", TALKER: "builder_w",
         CALLER_A: "caller_r", CALLER_B: "caller_g"}
PING = sid(0)


def m(mid: int, author: int, content: str, **extra) -> dict:
    return {"id": mid, "author_id": author, "author_name": NAMES[author], "content": content,
            "image_ids": [], **extra}


WINDOW = [
    m(sid(-32), TALKER, "Personally if i need a clear skill im first looking to frostblades"),
    m(sid(-24), SPAMMER, "https://planet.example.com/?shareCode=ABC&utm_source=share"),
    m(sid(-18), CALLER_A, "modssssssssssssssssss"),
    m(sid(-17), REPORTER, "isn't smite like..the best dps clear skill?"),
    m(sid(-15), CALLER_B, "nuke this mf"),
    m(sid(-12), TALKER, "If i need a lil more damage than that then smite of divine is better"),
    m(PING, REPORTER, "@Chat Moderator"),
]
BARE = {"user_id": REPORTER, "name": NAMES[REPORTER], "bare_ping": True}


def test_both_bystander_calls_land_on_the_link_not_the_reporter_or_each_other() -> None:
    callouts = bystander_callouts(WINDOW, REPORTER)
    assert len(callouts) == 1
    assert callouts[0]["about_message_id"] == sid(-24)
    assert [s["by"] for s in callouts[0]["said"]] == ["caller_r", "caller_g"]


def test_item_mods_talk_is_not_a_call_for_moderators() -> None:
    window = [
        m(sid(-20), TALKER, "https://tenor.com/view/cat-wave.gif"),
        m(sid(-10), CALLER_A, "rn i have 67% spell dmg can i keep my other mods"),
    ]
    assert bystander_callouts(window, REPORTER) == []


def test_a_bare_mods_after_a_gif_does_not_point_at_the_gif() -> None:
    window = [
        m(sid(-30), TALKER, "this build is fine"),
        m(sid(-20), CALLER_B, "https://tenor.com/view/cat-wave.gif"),
        m(sid(-10), CALLER_A, "mods"),
    ]
    assert bystander_callouts(window, REPORTER)[0]["about_message_id"] == sid(-30)


def test_without_audit_the_guess_is_still_the_nearest_message() -> None:
    out = reported_message_pointer(BARE, WINDOW, PING)
    assert out["likely_reported_message_id"] == sid(-12)
    assert "pointer_basis" not in out


def test_a_ban_on_someone_who_posted_just_before_the_ping_moves_the_pointer() -> None:
    out = reported_message_pointer(BARE, WINDOW, PING, ["Banned spam_user_d · by mod_b"])
    assert out["likely_reported_message_id"] == sid(-24)
    assert out["likely_reported_message"]["author_name"] == "spam_user_d"
    assert "pointer_basis" in out


def test_a_ban_never_overrides_a_reply_ping() -> None:
    window = [*WINDOW[:-1], m(PING, REPORTER, "@Chat Moderator", reply_to=sid(-32))]
    reporter = {**BARE, "bare_ping": False}
    out = reported_message_pointer(reporter, window, PING, ["Banned spam_user_d · by mod_b"])
    assert out["replied_to_message_id"] == sid(-32)
    assert "pointer_basis" not in out


def test_an_old_ban_on_a_long_ago_poster_does_not_move_the_pointer() -> None:
    window = [m(sid(-900), SPAMMER, "hello"), *WINDOW[-2:]]
    out = reported_message_pointer(BARE, window, PING, ["Banned spam_user_d · by mod_b"])
    assert out["likely_reported_message_id"] == sid(-12)


def test_enforcement_lines_are_read_back_to_names() -> None:
    assert enforced_names([
        "Banned spam_user_d · by mod_b",
        "Timed out user_x for 24h · by mod_y",
        "Deleted 1 messages from user_z · by mod_c",
        "Banned user_s (Suspicious or spam account) · by a",
        "Timed out user_t for 7d (transphobia) · by 🌀",
        "Replied in channel: \"honestly I don't know\" · by mod_b",
    ]) == {"spam_user_d", "user_x", "user_z", "user_s", "user_t"}


def test_no_action_over_a_ban_in_the_window_is_a_conflict() -> None:
    payload = {"messages": WINDOW, "reporter": BARE, "audit_findings": ["Banned spam_user_d · by mod_b"]}
    assert enforcement_conflict({"recommendations": ["No action."]}, payload) == ["spam_user_d"]
    assert enforcement_conflict({"recommendations": ["Already handled."]}, payload) == []
    # A ban of someone who never posted here is not this brief's business.
    other = {**payload, "audit_findings": ["Banned someone_else · by mod_b"]}
    assert enforcement_conflict({"recommendations": ["No action."]}, other) == []
    # Nor is the reporter being acted on.
    reporter_banned = {**payload, "audit_findings": ["Timed out reporter_k for 1h · by mod_b"]}
    assert enforcement_conflict({"recommendations": ["No action."]}, reporter_banned) == []


# ---- related actions: someone mentioned, not present --------------------------


def entry(action, target_id, name, at, *, until=None, reason=None, by="mod_a"):
    return SimpleNamespace(
        action=action,
        target=SimpleNamespace(id=target_id, name=name, display_name=name),
        user=SimpleNamespace(display_name=by, name=by),
        created_at=at,
        reason=reason,
        after=SimpleNamespace(timed_out_until=until),
    )


class FakeGuild:
    def __init__(self, entries):
        self.entries = entries

    def audit_logs(self, **_kw):
        async def gen():
            for e in self.entries:
                yield e
        return gen()


async def test_a_timeout_of_a_mentioned_user_minutes_before_the_ping_is_reported() -> None:
    at = _snowflake_created_at(PING)
    guild = FakeGuild([
        entry(discord.AuditLogAction.member_update, 77, "user_t", at - timedelta(minutes=5),
              until=at + timedelta(hours=24), by="mod_c"),
        entry(discord.AuditLogAction.ban, 88, "not_mentioned", at),
        entry(discord.AuditLogAction.member_update, 77, "user_t", at, until=None),
    ])
    lines = await collect_related_mod_actions(guild, {77}, PING)
    assert lines == ["Timed out user_t for 1d · by mod_c · 5m before the ping"]


class BusyGuild:
    """Honours action and limit like Discord does, oldest first."""

    def __init__(self, entries):
        self.entries = entries

    def audit_logs(self, *, limit, after, action=None):
        async def gen():
            hits = [e for e in self.entries if e.created_at > after and (action is None or e.action == action)]
            for e in hits[:limit]:
                yield e
        return gen()


async def test_a_timeout_behind_a_flood_of_autorole_entries_is_still_found() -> None:
    at = _snowflake_created_at(PING)
    noise = [
        entry(discord.AuditLogAction.member_role_update, 1000 + i, f"new_{i}",
              at - timedelta(minutes=19) + timedelta(seconds=i))
        for i in range(250)
    ]
    timeout = entry(discord.AuditLogAction.member_update, 77, "user_t", at - timedelta(minutes=5),
                    until=at + timedelta(hours=24), by="mod_c")
    lines = await collect_related_mod_actions(BusyGuild([*noise, timeout]), {77}, PING)
    assert lines == ["Timed out user_t for 1d · by mod_c · 5m before the ping"]


# ---- evidence links ----------------------------------------------------------


class FakeChannel:
    id = 175
    guild = SimpleNamespace(id=174)

    def __init__(self, alive: set[int]):
        self.alive = alive

    async def fetch_message(self, mid):
        if mid not in self.alive:
            raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "Unknown Message")
        return SimpleNamespace(id=mid)

    def history(self, *, around, limit):
        async def gen():
            for mid in sorted(self.alive, key=lambda i: abs(i - around.id))[:limit]:
                yield SimpleNamespace(id=mid)
        return gen()


def bot() -> IncidentBot:
    return IncidentBot.__new__(IncidentBot)


def url(mid: int) -> str:
    return f"https://discord.com/channels/174/175/{mid}"


async def test_a_deleted_quote_jumps_to_the_nearest_message_still_there() -> None:
    quotes = [EvidenceQuote(quote="the spam", message_id=sid(-24))]
    channel = FakeChannel({sid(-32), sid(-18), PING})
    await bot()._link_evidence(
        quotes, live_links={}, channel=channel, known_messages=WINDOW, anchor_message_id=PING
    )
    assert quotes[0].link == url(sid(-18))
    assert quotes[0].deleted


async def test_a_quote_still_there_but_not_fetched_live_gets_its_own_link() -> None:
    quotes = [EvidenceQuote(quote="frostblades", message_id=sid(-32))]
    channel = FakeChannel({sid(-32)})
    await bot()._link_evidence(
        quotes, live_links={}, channel=channel, known_messages=WINDOW, anchor_message_id=PING
    )
    assert quotes[0].link == url(sid(-32)) and not quotes[0].deleted


async def test_a_quote_with_no_message_id_is_found_by_text_or_falls_back_to_the_ping() -> None:
    quotes = [
        EvidenceQuote(quote="nuke this mf"),
        EvidenceQuote(quote="words that appear nowhere in the window"),
    ]
    channel = FakeChannel({sid(-15), PING})
    await bot()._link_evidence(
        quotes, live_links={}, channel=channel, known_messages=WINDOW, anchor_message_id=PING
    )
    assert quotes[0].link == url(sid(-15))
    assert quotes[1].link == url(PING)


def test_the_card_shows_a_quote_even_without_a_link_and_marks_deleted_ones() -> None:
    result = IncidentResult(
        headline="h", summary="s", recommendations=["Already handled."], draft_message="",
        evidence_quotes=[
            EvidenceQuote(quote="gone", message_id=1, link=url(2), deleted=True),
            EvidenceQuote(quote="no link at all"),
        ],
    )
    embed = bot()._build_incident_embed(result)
    field = next(f for f in embed.fields if f.name == "Evidence")
    assert f'"gone" [jump]({url(2)}) (deleted' in field.value
    assert '"no link at all"' in field.value


# ---- the rolling log ---------------------------------------------------------


def test_the_rolling_log_writes_one_line_per_event_and_rotates(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRIEF_LOG_MAX_MB", "0.001")
    monkeypatch.setenv("BRIEF_LOG_FILES", "3")
    path = brief_log.configure(str(tmp_path))
    for i in range(40):
        brief_log.record("analysis", anchor_message_id=PING, n=i, payload={"x": "y" * 50})
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["briefs.jsonl", "briefs.jsonl.1", "briefs.jsonl.2"]
    last = json.loads(open(path).read().splitlines()[-1])
    assert last["event"] == "analysis" and last["n"] == 39 and "since_ping_s" in last
    assert (tmp_path / "briefs.jsonl").stat().st_mode & 0o077 == 0
    monkeypatch.setenv("BRIEF_LOG_FILES", "0")
    assert brief_log.configure(str(tmp_path)) is None
