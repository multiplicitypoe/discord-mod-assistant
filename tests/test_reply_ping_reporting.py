"""Three reply-pings from one person, a day apart, all came back "No action"
about the wrong message. Each ping was a Discord reply to the message being
reported, and each brief judged whatever was posted just before the ping
instead: a bystander's joke, the target's follow-up emote, the target's
next, unrelated line. None of the briefs noticed the pinger was baiting, or
that it was their fourth ping that night.

Names and ids here are made up. The shapes are the real windows.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

import incident_mod_bot.bot as bot_module
from incident_mod_bot.bot import (
    IncidentBot,
    _consistent_author_names,
    _ping_context_text,
    _ping_reporter,
    _with_likely_reported,
)
from incident_mod_bot.memory.store import MemoryStore
from incident_mod_bot.pipeline.incident import IncidentResult
from incident_mod_bot.pipeline.ping_context import (
    analysis_image_candidates,
    build_ping_history,
    format_ping_history,
    snowflake_at_ms,
)

T0 = 1_791_000_000_000  # ms, an arbitrary night in 2026


def sid(seconds: float, n: int = 0) -> int:
    """A snowflake for T0 + seconds, n to keep same-second ids apart."""
    return snowflake_at_ms(T0 + int(seconds * 1000)) + n


PINGER, TARGET, BYSTANDER, OTHER = 1001, 1002, 1003, 1004
NAMES = {PINGER: "user_a", TARGET: "user_b", BYSTANDER: "user_c", OTHER: "user_d"}


def m(mid: int, author: int, content: str, **extra) -> dict:
    return {"id": mid, "author_id": author, "author_name": NAMES[author], "content": content,
            "image_ids": [], **extra}


# Case one: the ping replies to user_b's mockery; user_c's joke lands between.
C1_IMAGE = sid(-26)
C1_TARGET = sid(-8)
C1_JOKE = sid(-8, 1)
C1_PING = sid(0)
CASE_ONE = [
    m(sid(-84), PINGER, "damn it I identified it by accident"),
    m(sid(-61), BYSTANDER, "#poe1-helpful-guides"),
    m(C1_IMAGE, TARGET, "unfortunately for this bozo", attachments=["role.png (180x40 image)"]),
    m(sid(-23), PINGER, "Whew", attachments=["screenshot.png (1920x1080 image)"]),
    m(C1_TARGET, TARGET, "imagine being so insufferable you actually get banned from poe channels"),
    m(C1_JOKE, BYSTANDER, "time to remove gen perms as well for being offtopic"),
    m(C1_PING, PINGER, "@Chat Moderator", reply_to=C1_TARGET),
    m(sid(16), OTHER, "Are you still like this? Bruh", reply_to=C1_PING),
]


def reporter(bare: bool = True) -> dict:
    return {"user_id": PINGER, "name": "user_a", "bare_ping": bare}


def test_a_reply_ping_reports_the_message_it_replied_to() -> None:
    out = _with_likely_reported(reporter(), CASE_ONE, C1_PING)
    assert out["replied_to_message_id"] == C1_TARGET
    assert out["reported_message"]["author_name"] == "user_b"
    assert "insufferable" in out["reported_message"]["content"]
    # Not the bystander's joke posted in between.
    assert "likely_reported_message_id" not in out


def test_the_target_authors_later_line_does_not_steal_the_report() -> None:
    # Case three: user_b said the trolling line, then three other lines went
    # by, then user_b posted something unrelated just before the ping.
    target, later, ping = sid(-16), sid(-4), sid(0)
    window = [
        m(target, TARGET, "2 am sitting on discord 'trolling' people by being unlikable"),
        m(sid(-13), BYSTANDER, "skyr mmm"),
        m(sid(-6), OTHER, "an onion", attachments=["onion.png (64x64 image)"]),
        m(later, TARGET, "Reevaluation time"),
        m(ping, PINGER, "@Chat Moderator", reply_to=target),
    ]
    out = _with_likely_reported(reporter(), window, ping)
    assert out["replied_to_message_id"] == target
    assert out["reported_message"]["content"].startswith("2 am")


def test_a_reply_ping_whose_explanation_comes_after_still_points_at_the_reply() -> None:
    # Case two: the ping is bare, the pinger's own text follows 9s later.
    target, emote, ping = sid(-16), sid(-9), sid(0)
    window = [
        m(target, TARGET, "Imagine being this insufferable", attachments=["blob.png (64x64 image)"]),
        m(emote, TARGET, "<:HoldingTears:1>"),
        m(ping, PINGER, "@Chat Moderator", reply_to=target),
        m(sid(9), PINGER, "They're either going to ban me or do their actual jobs eventually."),
    ]
    out = _with_likely_reported(reporter(), window, ping)
    assert out["replied_to_message_id"] == target
    assert out["reported_message"]["attachments"] == ["blob.png (64x64 image)"]


def test_a_ping_with_text_that_replies_still_points_at_its_target() -> None:
    window = CASE_ONE[:-2] + [m(C1_PING, PINGER, "@Chat Moderator this", reply_to=C1_TARGET)]
    assert _with_likely_reported(reporter(bare=False), window, C1_PING)[
        "replied_to_message_id"
    ] == C1_TARGET


def test_replying_to_yourself_falls_back_to_the_last_message_from_someone_else() -> None:
    own = sid(-5)
    window = [m(sid(-9), TARGET, "you're bad"), m(own, PINGER, "see above"),
              m(sid(0), PINGER, "@Chat Moderator", reply_to=own)]
    out = _with_likely_reported(reporter(), window, sid(0))
    assert "replied_to_message_id" not in out
    assert out["likely_reported_message_id"] == sid(-9)


def test_a_reply_target_older_than_the_window_comes_from_the_ping_itself() -> None:
    old = sid(-4000)
    rep = {**reporter(), "replied_to": {"id": old, "author_id": TARGET, "author_name": "user_b",
                                        "content": "older taunt"}}
    window = [m(sid(-3), BYSTANDER, "lol"), m(sid(0), PINGER, "@Chat Moderator", reply_to=old)]
    out = _with_likely_reported(rep, window, sid(0))
    assert out["replied_to_message_id"] == old
    assert out["reported_message"]["content"] == "older taunt"


def test_the_live_ping_records_what_it_replied_to() -> None:
    target_author = SimpleNamespace(id=TARGET, name="user_b")
    resolved = SimpleNamespace(author=target_author, clean_content="imagine being so insufferable")
    ping = SimpleNamespace(
        author=SimpleNamespace(id=PINGER, name="user_a"),
        content="<@&1>",
        type=discord.MessageType.reply,
        reference=SimpleNamespace(message_id=C1_TARGET, resolved=resolved),
    )
    rep = _ping_reporter(ping)
    assert rep["replied_to"] == {"id": C1_TARGET, "author_id": TARGET, "author_name": "user_b",
                                 "content": "imagine being so insufferable"}
    assert _ping_context_text(ping, "@Chat Moderator", "offtopic") == (
        "user_a pinged @Chat Moderator in #offtopic, replying to user_b"
    )


def test_a_deleted_reply_target_names_nobody() -> None:
    ping = SimpleNamespace(
        author=SimpleNamespace(id=PINGER, name="user_a"),
        content="<@&1>",
        type=discord.MessageType.reply,
        reference=SimpleNamespace(message_id=C1_TARGET, resolved=SimpleNamespace(id=C1_TARGET)),
    )
    assert _ping_reporter(ping)["replied_to"] == {"id": C1_TARGET}
    assert _ping_context_text(ping, "@Chat Moderator", "offtopic").endswith("#offtopic")


def test_the_reporter_keeps_one_name_across_payload_and_messages() -> None:
    window = [{"id": 1, "author_id": PINGER, "author_name": "user_a"},
              {"id": 2, "author_id": PINGER, "author_name": "user_a_username"}]
    names = {d["author_name"] for d in _consistent_author_names(window, {PINGER: "user_a"})}
    assert names == {"user_a"}


def test_images_nearest_the_report_come_first() -> None:
    rep = _with_likely_reported(reporter(), CASE_ONE, C1_PING)
    order = analysis_image_candidates(CASE_ONE, C1_PING, rep)
    # The reported message, the ping, then the reported author's own lines
    # next to it (where the role image is), then everyone else nearest first.
    assert order[:4] == [C1_TARGET, C1_PING, C1_IMAGE, C1_JOKE]
    assert order.index(C1_IMAGE) < order.index(sid(-23))


# Ping history, against a real sqlite store.

async def _store(tmp_path) -> MemoryStore:
    store = MemoryStore(str(tmp_path / "t.sqlite3"))
    await store.connect()
    return store


async def _add_brief(store, brief_id, ping_id, reply_to=None, who=PINGER, target_name="user_b"):
    msgs = [m(ping_id, who, "@Chat Moderator", **({"reply_to": reply_to} if reply_to else {}))]
    if reply_to:
        msgs.insert(0, {**m(reply_to, TARGET, "taunt"), "author_name": target_name})
    await store.save_incident_payload(brief_id, 9, {
        "messages": msgs, "anchor_message_id": ping_id,
        "reporter": {"user_id": who, "name": NAMES[who], "bare_ping": True},
    })


async def _handled(store, brief_id, recommended, at_ms):
    conn = store._require_conn()
    await conn.execute(
        "INSERT INTO enforcement_log (guild_id, channel_id, brief_message_id, mod_user_id, action,"
        " target_user_ids, target_message_ids, rule_ids, recommended, outcome, created_at)"
        " VALUES (9, 1, ?, 5, 'action_taken', '[]', '[]', '[]', ?, 'handled_externally', ?)",
        (brief_id, json.dumps(recommended), at_ms // 1000),
    )
    await conn.commit()


async def test_earlier_pings_are_counted_and_resolved_as_of_the_ping(tmp_path) -> None:
    store = await _store(tmp_path)
    try:
        h = 3600
        now = sid(0)
        await _add_brief(store, sid(-16 * h + 5), sid(-16 * h), reply_to=sid(-16 * h - 9))
        await _add_brief(store, sid(-80 * 60 + 5), sid(-80 * 60))
        await _add_brief(store, sid(-40 * 60 + 5), sid(-40 * 60), reply_to=sid(-40 * 60 - 9),
                         target_name="user_d")
        await _add_brief(store, sid(-30 * 60 + 5), sid(-30 * 60), who=OTHER)  # someone else
        await _add_brief(store, sid(-9 * 86400), sid(-9 * 86400 - 5))  # outside the window
        await _add_brief(store, sid(60), sid(55))  # after this ping
        await _handled(store, sid(-16 * h + 5), ["No action."], T0 - 15 * h * 1000)
        # Handled only after this ping: must not count yet.
        await _handled(store, sid(-40 * 60 + 5), ["No action."], T0 + 4 * h * 1000)

        earlier, outcomes = await store.list_earlier_pings(
            9, PINGER, before_message_id=now, window_s=7 * 86400
        )
        history = build_ping_history(earlier, outcomes, anchor_message_id=now)
    finally:
        await store.close()

    assert history["earlier_pings"] == 3
    assert [r["hours_before"] for r in history["earlier"]] == [0.7, 1.3, 16.0]
    assert history["earlier"][0] == {"hours_before": 0.7, "replying_to": "user_d",
                                     "brief": "not marked handled"}
    assert history["earlier"][2]["brief"] == "No action"
    assert format_ping_history(history) == (
        "4th mod ping in ~16h. Earlier briefs: not marked handled x2, No action"
    )


def test_one_ping_has_no_history_line() -> None:
    assert build_ping_history([], {}, anchor_message_id=sid(0)) is None
    assert format_ping_history(None) == ""


# The whole analysis, with the model mocked: what it is sent, and what the
# card shows.

def _png(color: str, size=(180, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


class FakeAttachment:
    def __init__(self, att_id: int, filename: str, data: bytes, size=(180, 40)) -> None:
        self.id = att_id
        self.filename = filename
        self.content_type = "image/png"
        self.size = len(data)
        self.width, self.height = size
        self._data = data

    async def read(self) -> bytes:
        return self._data


def _discord_msg(d: dict, attachments=()) -> SimpleNamespace:
    reply_to = d.get("reply_to")
    return SimpleNamespace(
        id=d["id"],
        author=SimpleNamespace(id=d["author_id"], name=d["author_name"], bot=False,
                               created_at=discord.utils.snowflake_time(d["author_id"] << 22),
                               joined_at=None, public_flags=None),
        content=d["content"],
        clean_content=d["content"],
        message_snapshots=[],
        attachments=list(attachments),
        embeds=[],
        stickers=[],
        type=discord.MessageType.reply if reply_to else discord.MessageType.default,
        reference=SimpleNamespace(message_id=reply_to, resolved=None) if reply_to else None,
        jump_url=f"https://discord.com/channels/9/1/{d['id']}",
    )


class FakeStore:
    async def get_rules_memory(self, guild_id):
        return None

    async def list_server_memory(self, guild_id, limit=5):
        return []

    async def summarize_enforcement(self, guild_id):
        return []

    async def list_user_profile_entries(self, guild_id, user_id):
        return []

    async def list_earlier_pings(self, guild_id, user_id, *, before_message_id, window_s):
        prior = sid(-40 * 60)
        return [(prior + 5, {"anchor_message_id": prior, "messages": [],
                             "reporter": {"user_id": PINGER}})], {prior + 5: "No action"}


MODEL_REPLY = {
    "headline": "Mocking a channel-banned user",
    "summary": "user_b mocked someone for being banned from the PoE channels; user_c joked about "
               "removing gen perms.",
    "participants": [{"user_id": TARGET, "name": "user_b", "role": "offender"}],
    "signals": [], "rule_refs": [], "recommendations": ["Check the pinger's own recent messages"],
    "draft_message": "", "reply_targets": [], "draft_replies": [], "confidence": 0.8,
    "evidence_quotes": [{"quote": "imagine being so insufferable", "message_id": str(C1_TARGET)}],
    "memory_suggestions": {"server_notes": [], "user_notes": []},
    "reporter_note": "user_a pinged in reply to a taunt; second ping in an hour.",
}


@pytest.fixture
def analysed(monkeypatch):
    sent: dict = {}

    class FakeResponses:
        def create(self, **kwargs):
            sent.update(kwargs)
            return SimpleNamespace(output_text=json.dumps(MODEL_REPLY), error=None)

    monkeypatch.setattr(bot_module, "create_client",
                        lambda key: SimpleNamespace(responses=FakeResponses(), close=lambda: None))

    bot = IncidentBot.__new__(IncidentBot)
    bot.settings = SimpleNamespace(openai_api_key="x", openai_model="gpt-4.1-mini",
                                   openai_image_detail="low", openai_max_image_dim=512,
                                   debug_logs=False)
    bot.memory_store = FakeStore()
    att = {
        C1_IMAGE: [FakeAttachment(11, "role.png", _png("red"))],
        sid(-23): [FakeAttachment(12, "screenshot.png", _png("blue", (1920, 1080)), (1920, 1080))],
    }
    plain = [{k: v for k, v in d.items() if k != "attachments"} for d in CASE_ONE[:-1]]
    messages = [_discord_msg(d, att.get(d["id"], ())) for d in plain]
    return bot, messages, sent


async def test_the_analysis_is_sent_the_reply_target_and_the_images_near_it(analysed) -> None:
    bot, messages, sent = analysed
    result, raw, payload = await bot._analyze_incident_messages(
        guild_id=9, messages=messages, mod_role_id=None, anchor_message_id=C1_PING,
        ctx="test", reporter=reporter(),
    )
    by_id = {d["id"]: d for d in payload["messages"]}

    assert payload["reporter"]["replied_to_message_id"] == C1_TARGET
    assert payload["reporter"]["reported_message"]["author_name"] == "user_b"
    # The joke stays with whoever made it.
    assert by_id[C1_JOKE]["author_name"] == "user_c" and "reply_to" not in by_id[C1_JOKE]
    assert by_id[C1_PING]["reply_to"] == C1_TARGET

    # Images: the reported author's role image first, then the next one out.
    assert by_id[C1_IMAGE]["attachments"] == ["role.png (180x40 image)"]
    assert by_id[C1_IMAGE]["image_ids"] == [f"img_{C1_IMAGE}_1"]
    assert by_id[sid(-23)]["image_ids"] == [f"img_{sid(-23)}_2"]
    content = sent["input"][0]["content"]
    assert [c["type"] for c in content] == ["input_text", "input_text", "input_image",
                                            "input_text", "input_image"]
    assert content[1]["text"] == f"Image id: img_{C1_IMAGE}_1"
    assert content[2]["detail"] == "low"
    assert result.analysed_media == ["att:11", "att:12"]

    prompt = content[0]["text"]
    assert "IS the reported message" in prompt
    assert "You never decide a punishment for the reporter" in prompt
    assert "reporter_note" in prompt

    assert payload["reporter"]["recent_pings"]["earlier_pings"] == 1
    assert result.ping_history == "2nd mod ping in the last hour. Earlier briefs: No action"
    assert result.reporter_note.startswith("user_a pinged in reply")


async def test_the_card_shows_the_pinger_line(analysed) -> None:
    bot, messages, _ = analysed
    result, _, _ = await bot._analyze_incident_messages(
        guild_id=9, messages=messages, mod_role_id=None, anchor_message_id=C1_PING,
        ctx="test", reporter=reporter(),
    )
    embed = bot._build_incident_embed(result, context=("user_a pinged @Chat Moderator in "
                                                       "#offtopic, replying to user_b", None))
    assert embed.title.endswith("replying to user_b")
    assert "**Pinger:** user_a pinged in reply to a taunt; second ping in an hour. 2nd mod ping" \
        in embed.description
    assert embed.description.index("**What:**") < embed.description.index("**Pinger:**") \
        < embed.description.index("**Do:**")


async def test_images_are_capped(analysed, monkeypatch) -> None:
    bot, messages, sent = analysed
    many = [FakeAttachment(20 + i, f"{i}.png", _png("green")) for i in range(5)]
    messages[4].attachments = many  # the reported message itself
    _, _, payload = await bot._analyze_incident_messages(
        guild_id=9, messages=messages, mod_role_id=None, anchor_message_id=C1_PING,
        ctx="test", reporter=reporter(),
    )
    images = [c for c in sent["input"][0]["content"] if c["type"] == "input_image"]
    assert len(images) == IncidentBot._ANALYSIS_MAX_IMAGES
    assert sum(len(d["image_ids"]) for d in payload["messages"]) == len(images)


async def test_a_failed_download_leaves_a_text_only_analysis(analysed) -> None:
    bot, messages, sent = analysed

    class Broken(FakeAttachment):
        async def read(self) -> bytes:
            raise discord.HTTPException(SimpleNamespace(status=404, reason="gone"), "gone")

    for msg in messages:
        msg.attachments = [Broken(a.id, a.filename, b"") for a in msg.attachments]
    result, _, payload = await bot._analyze_incident_messages(
        guild_id=9, messages=messages, mod_role_id=None, anchor_message_id=C1_PING,
        ctx="test", reporter=reporter(),
    )
    assert [c["type"] for c in sent["input"][0]["content"]] == ["input_text"]
    assert all(not d["image_ids"] for d in payload["messages"])
    assert result.analysed_media == []


def test_new_result_fields_have_safe_defaults() -> None:
    r = IncidentResult(summary="x", draft_message="")
    assert (r.reporter_note, r.ping_history, r.analysed_media) == ("", "", [])
    assert "ping_history" not in r.model_dump() and "analysed_media" not in r.model_dump()


def test_an_unanswered_request_cannot_hold_a_brief_for_half_an_hour() -> None:
    from incident_mod_bot.openai_client import create_client

    client = create_client("sk-test")
    assert client.timeout == 90.0 and client.max_retries == 1


def test_a_control_byte_the_model_put_in_a_name_is_dropped() -> None:
    from incident_mod_bot.pipeline.incident import parse_incident_result

    r = parse_incident_result({**MODEL_REPLY, "summary": "user_a (\x02) pinged",
                               "reporter_note": "user_a (\x02) again"})
    assert r.summary == "user_a () pinged" and r.reporter_note == "user_a () again"
