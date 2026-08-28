"""Regenerate a brief for a historical incident, offline.

Built for iterating on the analyze_incident prompt in openai_client.py
without waiting for a live ping to test a change against.

    python -m incident_mod_bot.replay <brief message link>

Paste the link to the Mod bot's own brief message (the one posted in
chat-discussion, not the original ping) and, if it was posted after this
feature shipped, this replays the *exact* payload that brief was built from
- straight to the OpenAI API, no Discord calls at all. That makes it immune
to the thing that broke every earlier attempt at this: moderation incidents
get their evidence deleted as part of being handled, so replaying against
live channel history usually replays the wrong incident by the time anyone
gets around to it. See save_incident_payload in memory/store.py.

If no saved payload exists for that message id - an older brief, from before
this shipped, or a stray id - falls back to reconstructing the window from
live channel history, treating the id as the original anchor/ping message.
This only works if that history and its evidence are still there.

    python -m incident_mod_bot.replay <message id> --guild G --channel C

A full discord.com/channels/{guild}/{channel}/{message} link carries the
guild and channel already; a bare id needs both flags (only meaningful for
the live-history fallback, since a saved payload already carries its own
guild).

Read-only: never posts or edits anything in Discord. The live-history
fallback uses Client.login() rather than start()/connect(), so it never
opens a second gateway session on top of the running bot's. Talks to the
same sqlite db the live bot uses, read-only queries only, safe to run
concurrently with it. Meant to run inside the already-built image, which has
every dependency and the right env already:

    sudo -n docker run --rm --env-file .env -v $(pwd)/data:/app/data \\
        --user "$(id -u):$(id -g)" discord-incident-assistant \\
        python -m incident_mod_bot.replay <link>

Does not run the image-refinement pass (refine_incident_with_images) - text
only, which is where every wording complaint so far has landed. Pass --raw
to see the model's actual JSON alongside the rendered embed.
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
from incident_mod_bot.openai_client import OpenAISettings, analyze_incident, create_client
from incident_mod_bot.pipeline.incident import parse_incident_result

_LINK_RE = re.compile(r"discord\.com/channels/(\d+)/(\d+)/(\d+)")


async def populate_role_cache(guild: discord.Guild) -> None:
    """login()-only sessions never get the gateway events that normally
    populate a guild's role cache, so any message fetched this way resolves
    real, still-existing role mentions as the literal string "@deleted-role"
    (Message.clean_content's fallback for a role it can't find in cache) -
    in the analysis input itself, not just anything cosmetic. fetch_roles()
    gets the real roles over REST but, in this discord.py version, doesn't
    write them into guild._roles on its own; done here by hand so every
    message sharing this Guild object resolves correctly for the rest of
    the run.
    """
    try:
        roles = await guild.fetch_roles()
    except (discord.Forbidden, discord.HTTPException):
        print("warning: could not fetch roles - mentions may render as 'deleted role'", file=sys.stderr)
        return
    guild._roles = {r.id: r for r in roles}


def _parse_target(
    raw: str, guild_arg: int | None, channel_arg: int | None
) -> tuple[int | None, int | None, int]:
    m = _LINK_RE.search(raw)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
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


async def _replay_from_saved_payload(
    bot: IncidentBot, guild_id: int, payload: dict, *, show_raw: bool
) -> None:
    settings = bot.settings
    channel_id = payload.pop("source_channel_id", None)
    openai_settings = OpenAISettings(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        image_detail=settings.openai_image_detail,
        debug_logs=settings.debug_logs,
    )
    client = create_client(settings.openai_api_key)
    try:
        raw = await asyncio.to_thread(analyze_incident, client, openai_settings, payload)
    finally:
        try:
            client.close()
        except Exception:
            pass
    result = parse_incident_result(raw)
    if channel_id is not None:
        for q in result.evidence_quotes:
            if not q.link and q.message_id:
                q.link = f"https://discord.com/channels/{guild_id}/{channel_id}/{q.message_id}"

    n_msgs = len(payload.get("messages") or [])
    scan_label = f"{n_msgs} msgs | replay from saved payload, no image pass"
    embed = bot._build_incident_embed(result, scan_label=scan_label)
    print(_render(embed, scan_label=scan_label))
    if show_raw:
        print("\n--- raw model JSON ---")
        print(json.dumps(raw, indent=2))


async def _replay_from_live_history(
    bot: IncidentBot,
    guild_id: int | None,
    channel_id: int | None,
    message_id: int,
    *,
    limit: int | None,
    show_raw: bool,
) -> None:
    if guild_id is None or channel_id is None:
        raise SystemExit("no saved payload for this id, and a bare id needs --guild and --channel")
    settings = bot.settings
    await bot.login(settings.discord_token)

    channel = await bot.fetch_channel(channel_id)
    await populate_role_cache(channel.guild)
    anchor = await channel.fetch_message(message_id)

    use_limit = limit or settings.default_limit
    messages = await bot._fetch_recent_messages_ending_at(
        channel, limit=use_limit, end_message=anchor
    )
    if not messages:
        raise SystemExit("no messages in window (was everything from a bot?)")

    guild_config = await bot.memory_store.get_guild_config(guild_id)
    mod_role_id = guild_config.get("mod_role_id") or settings.mod_role_id

    ctx = f"replay guild={guild_id} channel={channel_id} anchor={message_id}"
    result, raw, _payload = await bot._analyze_incident_messages(
        guild_id=guild_id,
        messages=messages,
        mod_role_id=mod_role_id,
        anchor_message_id=message_id,
        ctx=ctx,
    )

    scan_label = f"{len(messages)} msgs | replay from live history, no image pass"
    embed = bot._build_incident_embed(result, scan_label=scan_label)
    print(_render(embed, scan_label=scan_label))
    if show_raw:
        print("\n--- raw model JSON ---")
        print(json.dumps(raw, indent=2))


async def _run(args: argparse.Namespace) -> None:
    load_dotenv()
    settings = load_settings()
    guild_id, channel_id, message_id = _parse_target(args.target, args.guild, args.channel)

    bot = IncidentBot(settings)
    logged_in = False
    try:
        await bot.memory_store.connect()
        found = await bot.memory_store.get_incident_payload(message_id)
        if found is not None:
            payload_guild_id, payload = found
            print(
                f"(replaying from the saved payload for brief {message_id} - no Discord fetch needed)",
                file=sys.stderr,
            )
            await _replay_from_saved_payload(bot, payload_guild_id, payload, show_raw=args.raw)
            return

        print(
            f"(no saved payload for {message_id} - falling back to a live history fetch)",
            file=sys.stderr,
        )
        logged_in = True
        await _replay_from_live_history(
            bot, guild_id, channel_id, message_id, limit=args.limit, show_raw=args.raw
        )
    finally:
        if logged_in:
            await bot.close()
        else:
            await bot.memory_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("target", help="message link, or a bare message id with --guild/--channel")
    parser.add_argument("--guild", type=int, default=None, help="live-history fallback only")
    parser.add_argument("--channel", type=int, default=None, help="live-history fallback only")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="live-history fallback only: window size (default: DEFAULT_LIMIT)",
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
