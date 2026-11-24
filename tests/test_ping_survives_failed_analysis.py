"""Two mod pings in the same second, and the bot posted nothing for either.

The first took the channel's cooldown; the second was dropped by it without a
trace. Then the first's analysis came back from the model as degenerate output
(a hex string repeated until the token cap cut it off mid-value), which is not
JSON, raised, and was logged. No retry, no notice, the cooldown still held, and
the dropped ping covered by nothing.

Covered here: unusable output is retried; a failure posts a notice in the mod
channel, gives the cooldown back, and runs the ping the cooldown had turned away.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import incident_mod_bot.bot as bot_module
from incident_mod_bot.bot import IncidentBot
from incident_mod_bot.openai_client import OpenAISettings, analyze_incident

GOOD = {"headline": "h", "summary": "s", "recommendations": [], "draft_message": ""}
DEGENERATE = '{\n  "headline": "x",\n  "evidence_quotes": [{"link": "' + "d7a27cc8" * 60


class ScriptedResponses:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0

    def create(self, **_kw):
        self.calls += 1
        return SimpleNamespace(output_text=self.texts.pop(0), error=None)


def run_analysis(texts):
    responses = ScriptedResponses(texts)
    client = SimpleNamespace(responses=responses)
    settings = OpenAISettings(api_key="x", model="gpt-4.1-mini", image_detail="low")
    return responses, lambda: analyze_incident(client, settings, {"messages": []})


def test_unusable_output_is_retried_and_the_second_answer_is_used() -> None:
    responses, call = run_analysis([DEGENERATE, json.dumps(GOOD)])
    assert call()["headline"] == "h"
    assert responses.calls == 2


def test_it_gives_up_after_three_unusable_answers() -> None:
    responses, call = run_analysis([DEGENERATE, "", DEGENERATE])
    with pytest.raises(ValueError, match="invalid JSON|empty"):
        call()
    assert responses.calls == 3


def test_a_good_first_answer_costs_one_call() -> None:
    responses, call = run_analysis([json.dumps(GOOD)])
    call()
    assert responses.calls == 1


# ---- the cooldown and the dropped ping ---------------------------------------

SRC_ID, DEST_ID, GUILD_ID = 111, 222, 333


def ping(mid: int, user_id: int):
    return SimpleNamespace(
        id=mid, jump_url=f"https://discord.test/{mid}", author=SimpleNamespace(id=user_id, name=f"user{user_id}"),
        role_mentions=[SimpleNamespace(id=7, name="Chat Moderator")], mentions=[],
    )


@pytest.fixture
def auto(monkeypatch):
    src = MagicMock(spec=discord.TextChannel)
    src.id, src.name, src.category = SRC_ID, "poe1-general", None
    dest = MagicMock(spec=discord.TextChannel)
    dest.id, dest.name = DEST_ID, "chat-discussion"
    dest.send = AsyncMock()
    guild = MagicMock()
    guild.id, guild.name = GUILD_ID, "g"
    guild.get_channel.return_value = dest

    monkeypatch.setattr(bot_module, "_AUTO_MOD_SOURCE_TYPES", (type(src),))
    monkeypatch.setattr(bot_module, "_ping_context_text", lambda m, roles, chan: f"user pinged in #{chan}")

    bot = IncidentBot.__new__(IncidentBot)
    bot._auto_last_run, bot._auto_held = {}, {}
    bot.settings = SimpleNamespace(auto_mod_default_channel_id=DEST_ID, default_limit=50, max_limit=200)
    bot.memory_store = SimpleNamespace(
        get_auto_mod_config=AsyncMock(return_value={}),
        list_auto_mod_ignored_categories=AsyncMock(return_value=[]),
        list_auto_mod_routes=AsyncMock(return_value=[]),
    )
    bot._fetch_recent_messages_ending_at = AsyncMock(
        return_value=[SimpleNamespace(created_at=discord.utils.utcnow(), author=SimpleNamespace(id=1))]
    )
    monkeypatch.setattr(bot_module, "_ping_reporter", lambda m: None)
    monkeypatch.setattr(bot_module, "_resolved_reply", lambda m: [])

    def attach(msg):
        msg.guild, msg.channel = guild, src
        return msg

    return bot, dest, attach


async def test_a_failed_analysis_posts_a_notice_and_runs_the_ping_it_held(auto) -> None:
    bot, dest, attach = auto
    first, second = attach(ping(1, 10)), attach(ping(2, 20))
    analysed: list[int] = []

    async def analyse(**kw):
        analysed.append(kw["anchor_message_id"])
        if kw["anchor_message_id"] == 1:
            # The second ping arrives while the first is still being analysed.
            await bot._handle_auto_mod_ping(second, mod_role_id=None)
            assert bot._auto_held[(GUILD_ID, SRC_ID)] is second
        raise ValueError("analyze_incident: invalid JSON")

    bot._analyze_incident_messages = analyse
    await bot._handle_auto_mod_ping(first, mod_role_id=None)
    for _ in range(5):  # let the re-run of the held ping finish
        await __import__("asyncio").sleep(0)

    assert analysed == [1, 2], "the ping the cooldown turned away was never run"
    titles = [c.kwargs["embed"].title for c in dest.send.await_args_list]
    assert len(titles) == 2 and all("pinged in #poe1-general" in t for t in titles)
    urls = [c.kwargs["embed"].url for c in dest.send.await_args_list]
    assert urls == ["https://discord.test/1", "https://discord.test/2"]
    assert bot._auto_held == {}


async def test_a_failure_gives_the_cooldown_back(auto) -> None:
    bot, dest, attach = auto
    bot._analyze_incident_messages = AsyncMock(side_effect=ValueError("garbled"))

    await bot._handle_auto_mod_ping(attach(ping(1, 10)), mod_role_id=None)

    assert (GUILD_ID, SRC_ID) not in bot._auto_last_run
    assert dest.send.await_count == 1


async def test_a_ping_inside_the_cooldown_is_held_not_run(auto) -> None:
    bot, dest, attach = auto
    bot._analyze_incident_messages = AsyncMock()
    bot._auto_last_run[(GUILD_ID, SRC_ID)] = __import__("time").monotonic()

    await bot._handle_auto_mod_ping(attach(ping(2, 20)), mod_role_id=None)

    bot._analyze_incident_messages.assert_not_awaited()
    assert bot._auto_held[(GUILD_ID, SRC_ID)].id == 2
