"""A ban that deletes the images must not take the brief's understanding of them along.

A real brief: someone posted the same explicit image four times in thirteen
seconds, was pinged on, and the first brief read it correctly ("Repeated NSFW
image spam", nsfw + spam). A moderator banned the account a second later, and a
ban deletes the account's recent messages, attachments and all. Images are
fetched live on every analysis, so the rebuild Mark Handled triggers found
nothing to show the model, was told not to guess at files it was not shown, and
rewrote the card as "posted the same image attachment four times" with the nsfw
rule gone. Nothing else held what the first look had seen.

The fix keeps each look's headline, summary and rules with the stored payload
(prior_read), hands them back in when some attachment can no longer be shown,
and never lets a look that saw fewer images overwrite one that saw more.
"""
from __future__ import annotations

import discord
from types import SimpleNamespace

from incident_mod_bot.bot import _carry_prior_read
from incident_mod_bot.pipeline.incident import IncidentResult, RuleRef

from test_reply_ping_reporting import C1_PING, analysed, reporter  # noqa: F401  (fixture)


async def run(bot, messages, **kw):
    return await bot._analyze_incident_messages(
        guild_id=9, messages=messages, mod_role_id=None, anchor_message_id=C1_PING,
        ctx="test", reporter=reporter(), **kw,
    )


def prompt_text(sent) -> str:
    return sent["input"][0]["content"][0]["text"]


def break_downloads(messages) -> None:
    """What a ban looks like to the next look: the attachments are gone."""

    class Gone:
        def __init__(self, a):
            self.id, self.filename, self.size = a.id, a.filename, a.size
            self.content_type = a.content_type

        async def read(self) -> bytes:
            raise discord.HTTPException(SimpleNamespace(status=404, reason="gone"), "gone")

    for msg in messages:
        msg.attachments = [Gone(a) for a in msg.attachments]


async def test_a_look_that_sees_the_images_records_what_it_made_of_them(analysed) -> None:
    bot, messages, sent = analysed

    result, _, payload = await run(bot, messages)

    assert payload["prior_read"]["headline"] == result.headline
    assert payload["prior_read"]["summary"] == result.summary
    assert payload["prior_read"]["images_seen"] == 2
    # Nothing earlier to show the model on a first look.
    assert '"prior_read"' not in prompt_text(sent)


async def test_a_rebuild_after_the_images_are_gone_is_handed_the_earlier_read(analysed) -> None:
    bot, messages, sent = analysed
    earlier = {"headline": "Repeated NSFW image spam", "summary": "Posted the same explicit image four times.",
               "rules": ["nsfw", "spam"], "images_seen": 3}
    break_downloads(messages)

    _, _, payload = await run(bot, messages, prior_read=earlier)

    text = prompt_text(sent)
    assert '"prior_read"' in text and "Repeated NSFW image spam" in text and '"nsfw"' in text
    # The counter is bookkeeping for us, not for the model.
    assert "images_seen" not in text
    # And the weaker look does not overwrite the stronger one.
    assert payload["prior_read"] == earlier


async def test_the_earlier_read_is_not_shown_when_every_image_is_still_visible(analysed) -> None:
    bot, messages, sent = analysed
    earlier = {"headline": "Old headline", "summary": "s", "rules": [], "images_seen": 1}

    _, _, payload = await run(bot, messages, prior_read=earlier)

    assert "Old headline" not in prompt_text(sent)
    # A look that saw at least as much replaces it.
    assert payload["prior_read"]["images_seen"] == 2


def test_a_look_that_saw_fewer_images_keeps_the_earlier_read() -> None:
    earlier = {"headline": "Repeated NSFW image spam", "summary": "s", "rules": ["nsfw"], "images_seen": 3}
    weaker = IncidentResult(summary="posted the same image attachment", draft_message="",
                            headline="Repeated image posts", rule_refs=[RuleRef(id="spam", reason="")])
    assert _carry_prior_read(earlier, weaker, images_seen=0) is earlier
    assert _carry_prior_read(earlier, weaker, images_seen=2) is earlier


def test_an_equal_or_better_look_replaces_it_and_the_first_look_creates_it() -> None:
    now = IncidentResult(summary="sum", draft_message="", headline="head",
                         rule_refs=[RuleRef(id="nsfw", reason=""), RuleRef(id="spam", reason="")])
    assert _carry_prior_read(None, now, images_seen=1) == {
        "headline": "head", "summary": "sum", "rules": ["nsfw", "spam"], "images_seen": 1}
    earlier = {"headline": "old", "summary": "", "rules": [], "images_seen": 1}
    assert _carry_prior_read(earlier, now, images_seen=1)["headline"] == "head"
