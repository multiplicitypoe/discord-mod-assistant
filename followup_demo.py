"""One-off, read-only: show what the bare-ping follow-up system would produce
for the live .noreasons incident, using the real bot code. Never posts or
edits anything in Discord - only channel.history()/fetch_message() reads and
one OpenAI call per embed shown.
"""
from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

from incident_mod_bot.bot import (
    IncidentBot,
    _is_bare_ping,
    _BARE_PING_FOLLOWUP_MAX_MESSAGES,
    _BARE_PING_FOLLOWUP_SCAN_LIMIT,
)
from incident_mod_bot.config import load_settings

GUILD_ID = 174993814845521922
SOURCE_CHANNEL_ID = 1118165205025828884
ANCHOR_ID = 1542898179425443950


def render(embed, label: str) -> str:
    lines = [f"TITLE: {embed.title}", f"({label})"]
    if embed.url:
        lines.append(f"URL:   {embed.url}")
    lines.append("")
    lines.append(embed.description or "(no description)")
    for f in embed.fields:
        lines.append("")
        lines.append(f"[{f.name}]")
        lines.append(f.value)
    if embed.footer and embed.footer.text:
        lines.append("")
        lines.append(f"— {embed.footer.text}")
    return "\n".join(lines)


async def main() -> None:
    load_dotenv()
    settings = load_settings()
    bot = IncidentBot(settings)
    try:
        await bot.memory_store.connect()
        await bot.login(settings.discord_token)

        channel = await bot.fetch_channel(SOURCE_CHANNEL_ID)
        anchor = await channel.fetch_message(ANCHOR_ID)
        print(f"anchor author={anchor.author} bare_ping={_is_bare_ping(anchor)}", file=sys.stderr)

        max_limit = settings.max_limit
        use_limit = min(max(settings.default_limit, 1), max_limit)
        messages = await bot._fetch_recent_messages_ending_at(
            channel, limit=use_limit, end_message=anchor
        )
        print(f"base scan window: {len(messages)} messages", file=sys.stderr)

        guild_config = await bot.memory_store.get_guild_config(GUILD_ID)
        mod_role_id = guild_config.get("mod_role_id") or settings.mod_role_id

        base_result, _raw, _payload = await bot._analyze_incident_messages(
            guild_id=GUILD_ID,
            messages=messages,
            mod_role_id=mod_role_id,
            anchor_message_id=ANCHOR_ID,
            ctx="demo-base",
        )
        base_embed = bot._build_incident_embed(
            base_result,
            title="Auto Mod Brief",
            scan_label=f"{len(messages)} msgs | base scan, no follow-up",
            informed_by=base_result.informed_by,
        )
        print(render(base_embed, "BASE - what the original ping-time scan produced"))
        print("\n" + "=" * 70 + "\n")

        after_messages = [
            m
            async for m in channel.history(
                after=anchor, limit=_BARE_PING_FOLLOWUP_SCAN_LIMIT, oldest_first=True
            )
        ]
        follow_ups = [
            m for m in after_messages if m.author.id == anchor.author.id and not m.author.bot
        ][:_BARE_PING_FOLLOWUP_MAX_MESSAGES]
        print(f"follow-ups found: {len(follow_ups)}", file=sys.stderr)
        for m in follow_ups:
            print(f"  {m.created_at} {m.author}: {m.content!r}", file=sys.stderr)

        if not follow_ups:
            print("No follow-up messages from the reporter - nothing for the adjuster to add.")
            return

        augmented = messages + follow_ups
        new_result, _raw2, _payload2 = await bot._analyze_incident_messages(
            guild_id=GUILD_ID,
            messages=augmented,
            mod_role_id=mod_role_id,
            anchor_message_id=ANCHOR_ID,
            ctx="demo-followup",
        )
        new_embed = bot._build_incident_embed(
            new_result,
            title="Auto Mod Brief",
            scan_label=f"{len(augmented)} msgs | with bare-ping follow-up",
            informed_by=new_result.informed_by,
        )
        print(render(new_embed, "WITH FOLLOW-UP - what the new adjuster system would produce"))

        changed = bot._incident_signature(base_result) != bot._incident_signature(new_result)
        print(f"\nsignature changed: {changed}", file=sys.stderr)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
