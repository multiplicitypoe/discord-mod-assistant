"""Regenerate a brief for a historical incident, offline.

Built for iterating on the analyze_incident/refine_incident_with_images
prompts in openai_client.py without waiting for a live ping to test a change
against. Fetches the same message window the auto-mod trigger would have
used, runs it through the current prompt, and prints the embed it would
produce - read-only, never posts or edits anything in Discord.

    python -m incident_mod_bot.replay <message link>
    python -m incident_mod_bot.replay <message id> --guild G --channel C

A full discord.com/channels/{guild}/{channel}/{message} link carries the
guild and channel already; a bare message id needs both flags.

Runs against the real OpenAI API (costs a call) and the real Discord API
(read-only history fetches), using the same DISCORD_TOKEN and
OPENAI_API_KEY as the live bot. Uses Client.login() rather than
start()/connect(), so it never opens a second gateway session or touches
the running bot's connection - just HTTP calls. Meant to run inside the
already-built container (it has every dependency and the right env
already):

    sudo -n docker exec -it discord-incident-assistant \\
        python -m incident_mod_bot.replay <link>

Talks to the same sqlite db the live bot uses (rules/server/user memory,
enforcement norms), read-only queries only, which is safe to run
concurrently with the live bot.

Does not run the image-refinement pass (refine_incident_with_images) - this
is a text-first tool for iterating on the text prompt, which is where every
wording complaint so far has landed. Pass --raw to see the model's actual
JSON alongside the rendered embed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys

import discord
from dotenv import load_dotenv

from incident_mod_bot.bot import IncidentBot
from incident_mod_bot.config import load_settings

_LINK_RE = re.compile(r"discord\.com/channels/(\d+)/(\d+)/(\d+)")


def _parse_target(
    raw: str, guild_arg: int | None, channel_arg: int | None
) -> tuple[int, int, int]:
    m = _LINK_RE.search(raw)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    if guild_arg is None or channel_arg is None:
        raise SystemExit("a bare message id needs --guild and --channel")
    if not raw.isdigit():
        raise SystemExit(f"not a message link or a bare message id: {raw!r}")
    return guild_arg, channel_arg, int(raw)


def _render(embed: discord.Embed, *, scan_label: str) -> str:
    lines = [f"TITLE: {embed.title}", f"({scan_label})"]
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


async def _run(args: argparse.Namespace) -> None:
    load_dotenv()
    settings = load_settings()
    guild_id, channel_id, message_id = _parse_target(args.target, args.guild, args.channel)

    bot = IncidentBot(settings)
    await bot.login(settings.discord_token)
    try:
        await bot.memory_store.connect()

        channel = await bot.fetch_channel(channel_id)
        anchor = await channel.fetch_message(message_id)

        limit = args.limit or settings.default_limit
        messages = await bot._fetch_recent_messages_ending_at(
            channel, limit=limit, end_message=anchor
        )
        if not messages:
            raise SystemExit("no messages in window (was everything from a bot?)")

        guild_config = await bot.memory_store.get_guild_config(guild_id)
        mod_role_id = guild_config.get("mod_role_id") or settings.mod_role_id

        ctx = f"replay guild={guild_id} channel={channel_id} anchor={message_id}"
        result, raw = await bot._analyze_incident_messages(
            guild_id=guild_id,
            messages=messages,
            mod_role_id=mod_role_id,
            anchor_message_id=message_id,
            ctx=ctx,
        )

        scan_label = f"{len(messages)} msgs | replay, no image pass"
        embed = bot._build_incident_embed(result, scan_label=scan_label)
        print(_render(embed, scan_label=scan_label))
        if args.raw:
            print("\n--- raw model JSON ---")
            print(json.dumps(raw, indent=2))
    finally:
        await bot.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("target", help="message link, or a bare message id with --guild/--channel")
    parser.add_argument("--guild", type=int, default=None)
    parser.add_argument("--channel", type=int, default=None)
    parser.add_argument(
        "--limit", type=int, default=None, help="message window size (default: DEFAULT_LIMIT)"
    )
    parser.add_argument("--raw", action="store_true", help="also print the model's raw JSON")
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except discord.HTTPException as exc:
        print(f"Discord API error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
