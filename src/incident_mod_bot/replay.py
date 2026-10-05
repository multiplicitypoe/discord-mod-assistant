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
        --user "$(id -u):$(id -g)" discord-mod-assistant \\
        python -m incident_mod_bot.replay <link>

A saved payload is replayed the way the current code would build it from
the same messages: the reporter's pointer to the reported message is
recomputed, and their earlier pings are read from the same db as of the
ping. --as-saved sends the stored payload untouched instead.

--images also sends the analysis pass the images the live bot would pick
(the reported message, the ping, and the messages next to it). Images are
never stored, so this logs in over REST (Client.login(), no gateway) and
fetches those messages and their attachments: reads only. If they have been
deleted since, it says so and runs on text. It does not run the background
refinement pass (refine_incident_with_images).

--raw prints the model's actual JSON alongside the rendered embed, and
--show-payload the exact payload sent (image bytes left out).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time

import discord
from dotenv import load_dotenv

from incident_mod_bot.bot import IncidentBot, _consistent_author_names, _with_likely_reported
from incident_mod_bot.config import load_settings
from incident_mod_bot.openai_client import OpenAISettings, analyze_incident, create_client
from incident_mod_bot.pipeline.incident import known_users_from_payload, parse_incident_result
from incident_mod_bot.pipeline.ping_context import format_ping_history

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
    bot: IncidentBot,
    guild_id: int,
    payload: dict,
    *,
    show_raw: bool,
    as_saved: bool = False,
    with_images: bool = False,
    show_payload: bool = False,
) -> None:
    settings = bot.settings
    channel_id = payload.pop("source_channel_id", None)
    anchor_id = payload.get("anchor_message_id")
    reporter = payload.get("reporter")
    if reporter and not as_saved:
        # What the current code builds from the same messages.
        payload["messages"] = _consistent_author_names(
            payload.get("messages") or [], {reporter.get("user_id"): reporter.get("name")}
        )
        payload["reporter"] = _with_likely_reported(reporter, payload["messages"], anchor_id)
        if isinstance(anchor_id, int):
            history = await bot._ping_history(guild_id, payload["reporter"], anchor_id)
            if history:
                payload["reporter"]["recent_pings"] = history

    images: list[dict] = []
    if with_images:
        if channel_id is None:
            print("warning: payload has no source channel; no images", file=sys.stderr)
        else:
            await bot.login(settings.discord_token)
            channel = await bot.fetch_channel(channel_id)
            t0 = time.monotonic()
            # One history call for the neighbourhood, like the live bot
            # already holding the window; single fetches only for the rest.
            live: dict[int, discord.Message] = {}
            if isinstance(anchor_id, int):
                try:
                    async for msg in channel.history(around=discord.Object(anchor_id), limit=21):
                        live[msg.id] = msg
                except discord.HTTPException:
                    pass
            images, used = await bot._collect_analysis_images(
                payload, live=live, fetch_message=channel.fetch_message, fetch_unlisted=True
            )
            print(
                f"(images: {len(images)} sent, from {sorted(used)}, "
                f"{sum(len(i['data_url']) for i in images)} data-url chars, "
                f"collected in {time.monotonic() - t0:.2f}s)",
                file=sys.stderr,
            )

    if show_payload:
        print("--- payload sent ---")
        print(json.dumps(payload, indent=1, ensure_ascii=False))
        print("--- end payload ---")

    openai_settings = OpenAISettings(
        api_key=settings.openai_api_key,
        model=settings.openai_model,
        image_detail=settings.openai_image_detail,
        debug_logs=settings.debug_logs,
    )
    client = create_client(settings.openai_api_key)
    t0 = time.monotonic()
    try:
        raw = await asyncio.to_thread(
            analyze_incident, client, openai_settings, payload, images or None
        )
    finally:
        try:
            client.close()
        except Exception:
            pass
    print(f"(analyze_incident took {time.monotonic() - t0:.2f}s)", file=sys.stderr)
    result = parse_incident_result(raw, known_users_from_payload(payload))
    result.ping_history = format_ping_history((payload.get("reporter") or {}).get("recent_pings"))
    if channel_id is not None:
        for q in result.evidence_quotes:
            if not q.link and q.message_id:
                q.link = f"https://discord.com/channels/{guild_id}/{channel_id}/{q.message_id}"

    n_msgs = len(payload.get("messages") or [])
    mode = "as saved" if as_saved else "rebuilt by current code"
    scan_label = f"{n_msgs} msgs | replay from saved payload ({mode}), {len(images)} image(s)"
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
            logged_in = args.images
            await _replay_from_saved_payload(
                bot,
                payload_guild_id,
                payload,
                show_raw=args.raw,
                as_saved=args.as_saved,
                with_images=args.images,
                show_payload=args.show_payload,
            )
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
        # bot.close() doesn't close the store, and an open aiosqlite
        # connection's worker thread keeps the process alive.
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
    parser.add_argument(
        "--as-saved",
        action="store_true",
        help="saved payload only: send it untouched, without recomputing the reporter fields",
    )
    parser.add_argument(
        "--images",
        action="store_true",
        help="saved payload only: fetch (read-only) and send the images the live bot would pick",
    )
    parser.add_argument(
        "--show-payload", action="store_true", help="print the exact payload sent to the model"
    )
    args = parser.parse_args()
    try:
        asyncio.run(_run(args))
    except discord.HTTPException as exc:
        print(f"Discord API error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
