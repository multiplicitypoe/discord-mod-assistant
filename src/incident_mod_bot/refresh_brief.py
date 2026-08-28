"""Show, and optionally apply, what _maybe_update_brief_with_followup would
produce for a posted brief - using the real analysis and embed-building code,
not a hand-simulated version.

Built for the .noreasons incident: a brief handled by a moderator can still
be worth refreshing with what happened in the channel right after the ping,
and this lets that be checked (or applied) after the fact instead of only
ever running automatically in the 60s window right after a bare ping.

    python -m incident_mod_bot.refresh_brief <brief message link>

Read-only by default: fetches the anchor ping, the same scan window the
original brief used, and everyone's messages in the channel after the ping
(the same widened window _maybe_update_brief_with_followup itself now scans -
see _BARE_PING_FOLLOWUP_SCAN_LIMIT), runs one fresh analysis, and prints the
result next to what the live message currently shows. Nothing is sent to
Discord.

    python -m incident_mod_bot.refresh_brief <brief message link> --apply

Applies it for real: edits the live message with the new embed. If the brief
was already marked handled, the result is treated exactly like pressing Mark
Handled would - green, no Draft reply field, the original "Marked Handled by
X" attribution carried forward (_apply_handled_look) - and the view stays
handled, so this never reopens a resolved brief. The stored view record and
incident payload are updated too, so a later replay or restart sees the
refreshed content.

Requires a brief that has both a saved ViewRecord (for mod_role_id,
allow_post/allow_actions, source_channel_id) and an anchor_message_id (the
ping or flagged message that triggered it) - a manually-run /mod scan has no
single anchor and can't be refreshed this way.

Meant to run inside the already-built image, same as replay.py:

    sudo -n docker run --rm --env-file .env -v $(pwd)/data:/app/data \\
        --user "$(id -u):$(id -g)" discord-incident-assistant \\
        python -m incident_mod_bot.refresh_brief <link> [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time

import discord
from dotenv import load_dotenv

from incident_mod_bot.bot import (
    IncidentBot,
    _BARE_PING_FOLLOWUP_SCAN_LIMIT,
    display_name,
    is_mod,
)
from incident_mod_bot.config import load_settings
from incident_mod_bot.discord_ui.incident_view import IncidentViewPayload
from incident_mod_bot.discord_ui.view_store import ViewRecord

_LINK_RE = re.compile(r"discord\.com/channels/(\d+)/(\d+)/(\d+)")


def _render(embed: discord.Embed, *, label: str) -> str:
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


async def _run(target: str, *, apply: bool) -> None:
    m = _LINK_RE.search(target)
    if not m:
        raise SystemExit(f"not a discord.com/channels/... message link: {target!r}")
    guild_id, dest_channel_id, brief_message_id = (int(g) for g in m.groups())

    load_dotenv()
    settings = load_settings()
    bot = IncidentBot(settings)
    logged_in = False
    try:
        await bot.memory_store.connect()
        await bot.view_store.connect()
        logged_in = True
        await bot.login(settings.discord_token)

        existing_records = await bot.view_store.load_views()
        existing = next((r for r in existing_records if r.message_id == brief_message_id), None)
        if existing is None:
            raise SystemExit(
                f"no stored view record for {brief_message_id} - "
                "either it predates view persistence, or the bot has restarted "
                "and this id was never restored"
            )
        existing_payload = existing.payload
        anchor_id = existing_payload.get("anchor_message_id")
        if not anchor_id:
            raise SystemExit(
                "this brief has no anchor_message_id - it came from a manually-run "
                "/mod scan with no single triggering message, so there's nothing to refresh from"
            )
        source_channel_id = existing_payload.get("source_channel_id")
        if not source_channel_id:
            raise SystemExit("stored payload has no source_channel_id")
        was_handled = bool(existing_payload.get("handled", False))
        mod_role_id = existing_payload.get("mod_role_id")

        dest_channel = await bot.fetch_channel(dest_channel_id)
        brief_message = await dest_channel.fetch_message(brief_message_id)
        source_channel = await bot.fetch_channel(source_channel_id)
        anchor = await source_channel.fetch_message(int(anchor_id))

        print(f"anchor: {anchor.author} @ {anchor.created_at}: {anchor.content!r}", file=sys.stderr)
        print(f"brief currently handled: {was_handled}", file=sys.stderr)

        max_limit = settings.max_limit
        use_limit = min(max(settings.default_limit, 1), max_limit)
        base_messages = await bot._fetch_recent_messages_ending_at(
            source_channel, limit=use_limit, end_message=anchor
        )
        if not base_messages:
            raise SystemExit("no messages in the base scan window")

        follow_ups = [
            msg
            async for msg in source_channel.history(
                after=anchor, limit=_BARE_PING_FOLLOWUP_SCAN_LIMIT, oldest_first=True
            )
            if not msg.author.bot
        ]
        print(f"post-ping messages found: {len(follow_ups)}", file=sys.stderr)
        for msg in follow_ups:
            print(f"  {msg.created_at} {msg.author}: {msg.content!r}", file=sys.stderr)
        if not follow_ups:
            print("Nothing happened in the channel after the ping - nothing to refresh.")
            return

        guild_config = await bot.memory_store.get_guild_config(guild_id)
        mod_role_id = mod_role_id or guild_config.get("mod_role_id") or settings.mod_role_id

        augmented = base_messages + follow_ups
        result, raw_result, analysis_payload = await bot._analyze_incident_messages(
            guild_id=guild_id,
            messages=augmented,
            mod_role_id=mod_role_id,
            anchor_message_id=int(anchor_id),
            ctx=f"refresh_brief {brief_message_id}",
        )

        source_parent = (
            source_channel.parent if isinstance(source_channel, discord.Thread) else source_channel
        )
        ping_author = anchor.author.display_name if isinstance(anchor.author, discord.Member) else str(anchor.author)
        role_names = ", ".join(f"@{r.name}" for r in anchor.role_mentions) or "the modmail bot"
        context = (
            f"{ping_author} pinged {role_names} in #{source_parent.name}",
            anchor.jump_url,
        )
        scan_label = f"{len(augmented)} msgs | refresh_brief, {len(follow_ups)} post-ping"

        new_embed = bot._build_incident_embed(
            result, title="Auto Mod Brief", scan_label=scan_label, context=context
        )
        if was_handled:
            bot._apply_handled_look(new_embed, brief_message)

        print(_render(new_embed, label="what the refresh produces"))

        if not apply:
            print("\n(dry run - pass --apply to edit the live message)", file=sys.stderr)
            return

        action_participants: list[dict] = []
        seen_users: set[int] = set()
        for msg in augmented:
            if msg.author.id in seen_users:
                continue
            seen_users.add(msg.author.id)
            member = msg.author if isinstance(msg.author, discord.Member) else None
            action_participants.append(
                {
                    "user_id": msg.author.id,
                    "name": display_name(msg.author),
                    "role": "mod" if is_mod(member, mod_role_id) else "member",
                }
            )
            if len(action_participants) >= 25:
                break

        new_payload = IncidentViewPayload(
            draft_message=result.draft_message,
            reply_targets=[t.model_dump() for t in result.reply_targets],
            draft_replies=[r.model_dump() for r in result.draft_replies],
            memory_suggestions=result.memory_suggestions.model_dump(),
            mod_role_id=mod_role_id,
            participants=action_participants,
            evidence_quotes=[q.model_dump() for q in result.evidence_quotes],
            recommendations=list(result.recommendations or []),
            rule_ids=[r.id for r in (result.rule_refs or [])],
            source_channel_id=source_channel_id,
            allow_post=bool(existing_payload.get("allow_post", True)),
            allow_actions=bool(existing_payload.get("allow_actions", True)),
            anchor_message_id=int(anchor_id),
            handled=was_handled,
        )

        await brief_message.edit(embed=new_embed)
        print("\nEDIT APPLIED", file=sys.stderr)

        record = ViewRecord(
            message_id=brief_message.id,
            channel_id=brief_message.channel.id,
            guild_id=guild_id,
            payload=new_payload.to_dict(),
            created_at=time.time(),
        )
        await bot.view_store.save_view(record)

        try:
            await bot.memory_store.save_incident_payload(
                brief_message.id,
                guild_id,
                {**analysis_payload, "source_channel_id": source_channel_id},
            )
        except Exception:
            print("warning: could not save updated incident payload", file=sys.stderr)
    finally:
        if logged_in:
            await bot.close()
        await bot.view_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("target", help="link to the Mod bot's own brief message")
    parser.add_argument(
        "--apply", action="store_true", help="edit the live message instead of only printing"
    )
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.target, apply=args.apply))
    except discord.HTTPException as exc:
        print(f"Discord API error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
