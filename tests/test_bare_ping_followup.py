"""A bare mod ping was judged on "the last message before the ping", even when
the reporter's own follow-up ("likely bot in chat") and other members
("same prompt ahh bot") pointed at a scripted greeting 30 messages earlier.
The model gave "no violation" at 0.98 on an unrelated build chat line.

Names and ids are made up. The shape is the real window.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from incident_mod_bot.bot import _with_likely_reported
from incident_mod_bot.openai_client import OpenAISettings, analyze_incident
from incident_mod_bot.pipeline.ping_context import (
    bystander_callouts,
    reporter_followup_text,
    snowflake_at_ms,
)

T0 = 1_791_000_000_000


def sid(seconds: float, n: int = 0) -> int:
    return snowflake_at_ms(T0 + int(seconds * 1000)) + n


REPORTER, BOT, SKEPTIC, CHATTER_A, CHATTER_B, LINKER = 2001, 2002, 2003, 2004, 2005, 2006
NAMES = {
    REPORTER: "user_a", BOT: "user_bot", SKEPTIC: "user_c",
    CHATTER_A: "user_d", CHATTER_B: "user_e", LINKER: "user_f",
}


def m(mid: int, author: int, content: str, **extra) -> dict:
    return {"id": mid, "author_id": author, "author_name": NAMES[author], "content": content,
            "image_ids": [], **extra}


GREETING = sid(-900)
LAST_BEFORE_PING = sid(-5)
PING = sid(0)
FOLLOWUP = sid(40)
WINDOW = [
    m(sid(-1000), CHATTER_A, "axe or sword?"),
    m(GREETING, BOT, "Hello I'm new here I don't go out really much I'm open to making new "
                     "discord friends, pls be friendly"),
    m(sid(-890), SKEPTIC, "getting a little sussy"),
    m(sid(-885), SKEPTIC, "same prompt ahh bot"),
    m(sid(-880), CHATTER_B, "https://example.com/gifs/cat-wave"),
    m(sid(-870), LINKER, "https://example.com/gifs/hello-cats"),
    m(sid(-860), SKEPTIC, "2nd bot to ignore me"),
    m(sid(-600), CHATTER_A, "its like what, 1 bill dps minimum?"),
    m(LAST_BEFORE_PING, CHATTER_A, "its tanky enough"),
    m(PING, REPORTER, "@Chat Moderator"),
    m(sid(10), CHATTER_A, "its like what"),
    m(FOLLOWUP, REPORTER, "likely bot in chat"),
]
REPORTER_DICT = {"user_id": REPORTER, "name": "user_a", "bare_ping": True}


def reporter() -> dict:
    return _with_likely_reported(dict(REPORTER_DICT), WINDOW, PING)


def test_the_reporters_follow_up_reaches_the_model_as_a_field() -> None:
    assert reporter()["followup_text"] == [{"id": FOLLOWUP, "text": "likely bot in chat"}]


def test_the_bare_ping_guess_has_no_reply_target_authority() -> None:
    out = reporter()
    assert out["likely_reported_message_id"] == LAST_BEFORE_PING
    assert out["likely_reported_message"]["content"] == "its tanky enough"
    assert "reported_message" not in out


def test_bystander_callouts_point_at_the_message_they_are_about() -> None:
    callouts = reporter()["callouts"]
    greeting = [c for c in callouts if c["about_message_id"] == GREETING]
    assert len(greeting) == 1
    assert greeting[0]["about_author"] == "user_bot"
    said = " | ".join(s["text"] for s in greeting[0]["said"])
    assert "sussy" in said and "ahh bot" in said
    # Link-only posts are skipped when finding what a callout is about, so
    # "2nd bot to ignore me" does not point at a cat GIF.
    assert all(c["about_message_id"] not in (sid(-880), sid(-870)) for c in callouts)


def test_a_callout_that_is_a_reply_points_at_its_reply_target() -> None:
    window = [
        m(sid(-30), CHATTER_A, "send me your dms for free stuff"),
        m(sid(-20), CHATTER_B, "unrelated"),
        m(sid(-10), SKEPTIC, "this is a scam", reply_to=sid(-30)),
    ]
    assert bystander_callouts(window, REPORTER)[0]["about_message_id"] == sid(-30)


def test_the_reporters_own_messages_are_never_callouts() -> None:
    window = [m(sid(-30), CHATTER_A, "hi"), m(sid(-20), REPORTER, "this is a bot")]
    assert bystander_callouts(window, REPORTER) == []


def test_follow_up_ignores_other_people_the_bare_mention_and_late_messages() -> None:
    window = [
        m(PING, REPORTER, "@Chat Moderator"),
        m(sid(5), CHATTER_A, "likely not a bot"),
        m(sid(6), REPORTER, "@Chat Moderator"),
        m(sid(7), REPORTER, "spam above"),
        m(sid(20 * 60), REPORTER, "ten minutes later, not part of the report"),
    ]
    assert reporter_followup_text(window, REPORTER, PING) == [{"id": sid(7), "text": "spam above"}]


def test_follow_up_is_capped() -> None:
    window = [m(PING, REPORTER, "@Chat Moderator")] + [
        m(sid(i + 1), REPORTER, f"line {i}") for i in range(8)
    ]
    assert len(reporter_followup_text(window, REPORTER, PING)) == 3


# The case that must not change: "mods" / ping / "kill him" about the line just above.
SPAMMER, KILLER = 3001, 3002
KILL_PING = sid(0)
KILL_WINDOW = [
    {"id": sid(-60), "author_id": SPAMMER, "author_name": "user_s",
     "content": "Hi wyd guys I'm bored ... Dms open", "image_ids": []},
    {"id": sid(-20), "author_id": KILLER, "author_name": "user_k", "content": "mods", "image_ids": []},
    {"id": KILL_PING, "author_id": KILLER, "author_name": "user_k",
     "content": "@Chat Moderator", "image_ids": []},
    {"id": sid(5), "author_id": KILLER, "author_name": "user_k", "content": "kill him", "image_ids": []},
]


def test_mods_kill_him_still_points_at_the_line_above() -> None:
    out = _with_likely_reported(
        {"user_id": KILLER, "name": "user_k", "bare_ping": True}, KILL_WINDOW, KILL_PING
    )
    assert out["likely_reported_message_id"] == KILL_WINDOW[0]["id"]
    assert out["followup_text"] == [{"id": KILL_WINDOW[3]["id"], "text": "kill him"}]
    assert "callouts" not in out


def test_a_reply_ping_still_points_at_its_reply_target_and_keeps_its_authority() -> None:
    target = sid(-300)
    window = [
        m(target, CHATTER_A, "imagine being this insufferable"),
        m(sid(-100), CHATTER_B, "a neighbouring line"),
        m(PING, REPORTER, "@Chat Moderator", reply_to=target),
    ]
    out = _with_likely_reported({"user_id": REPORTER, "name": "user_a", "bare_ping": True},
                                window, PING)
    assert out["replied_to_message_id"] == target
    assert out["reported_message"]["content"] == "imagine being this insufferable"
    assert "likely_reported_message_id" not in out


def test_stale_stored_fields_are_recomputed() -> None:
    stale = {**REPORTER_DICT, "followup_text": [{"id": 1, "text": "old"}],
             "callouts": [{"about_message_id": 1}], "reported_message": {"id": 1}}
    out = _with_likely_reported(stale, WINDOW, PING)
    assert out["followup_text"][0]["text"] == "likely bot in chat"
    assert "reported_message" not in out
    assert all(c["about_message_id"] != 1 for c in out["callouts"])


def _instructions() -> str:
    sent: dict = {}

    class FakeResponses:
        def create(self, **kwargs):
            sent.update(kwargs)
            return SimpleNamespace(output_text=json.dumps({
                "headline": "h", "summary": "s", "participants": [], "signals": [],
                "rule_refs": [], "recommendations": [], "draft_message": "",
                "reply_targets": [], "draft_replies": [], "confidence": 0.4,
                "evidence_quotes": [], "memory_suggestions": {"server_notes": [], "user_notes": []},
                "reporter_note": "",
            }), error=None)

    settings = OpenAISettings(api_key="x", model="gpt-4.1-mini", image_detail="low")
    analyze_incident(SimpleNamespace(responses=FakeResponses()), settings,
                     {"messages": [], "reporter": reporter()})
    content = sent["input"][0]["content"]
    return "\n".join(c["text"] for c in content if c.get("type") == "input_text")


def test_the_prompt_tells_the_model_what_the_new_fields_mean() -> None:
    text = _instructions()
    for needle in ("reporter.followup_text", "reporter.callouts", "it is only a guess",
                   "Never give high confidence to 'no violation'"):
        assert needle in text
