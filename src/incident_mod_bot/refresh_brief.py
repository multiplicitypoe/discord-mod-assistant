"""Show, and optionally apply, what a fresh evidence check would produce for
a posted brief - using Bot._refresh_incident_evidence, the exact function the
running bot itself calls after every auto-post and after every Mark Handled
press. Not a hand-simulated version: whatever this prints is what the live
bot would do too, so there is exactly one place that logic can drift.

Built for the reporter_n incident, later widened to also check the audit
log (the user_r/spam_user_j incident: a bare ping read as baseless spam
until the audit log showed the pinged-about account had already been
banned) - a brief handled by a moderator can still be worth refreshing
with what happened afterward, and this lets that be checked (or applied)
after the fact instead of only ever running automatically.

    python -m incident_mod_bot.refresh_brief <brief message link>

Read-only by default: looks for new channel messages and audit-log activity
since the last analysis, and if it finds any, runs one fresh analysis and
prints the result next to what the live message currently shows. Nothing is
sent to Discord, and nothing is persisted.

    python -m incident_mod_bot.refresh_brief <brief message link> --apply

Applies it for real: edits the live message with the new embed, updates the
stored view record, and grows the persisted incident payload with whatever
new evidence was found - so a later run (or the bot's own background polling)
picks up from here rather than rechecking from scratch. If the brief was
already marked handled, the result is treated exactly like pressing Mark
Handled would - green, no Draft reply field, the original "Marked Handled by
X" attribution carried forward - and the view stays handled, so this never
reopens a resolved brief.

Requires a brief that has both a saved ViewRecord (for mod_role_id,
allow_post/allow_actions, source_channel_id) and an anchor_message_id (the
ping or flagged message that triggered it) - a manually-run /mod scan has no
single anchor and can't be refreshed this way.

Meant to run inside the already-built image, same as replay.py:

    sudo -n docker run --rm --env-file .env -v $(pwd)/data:/app/data \\
        --user "$(id -u):$(id -g)" discord-mod-assistant \\
        python -m incident_mod_bot.refresh_brief <link> [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys

import discord
from dotenv import load_dotenv

from incident_mod_bot.bot import IncidentBot, _ping_context_text
from incident_mod_bot.config import load_settings
from incident_mod_bot.discord_ui.incident_view import IncidentView, IncidentViewPayload
from incident_mod_bot.replay import populate_role_cache

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


async def _run(target: str, *, apply: bool, force: bool = False) -> None:
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

        dest_channel = await bot.fetch_channel(dest_channel_id)
        brief_message = await dest_channel.fetch_message(brief_message_id)
        source_channel = await bot.fetch_channel(source_channel_id)
        await populate_role_cache(source_channel.guild)
        anchor = await source_channel.fetch_message(int(anchor_id))

        print(f"anchor: {anchor.author} @ {anchor.created_at}: {anchor.content!r}", file=sys.stderr)
        print(f"brief currently handled: {was_handled}", file=sys.stderr)

        guild_config = await bot.memory_store.get_guild_config(guild_id)
        mod_role_id = existing_payload.get("mod_role_id") or guild_config.get("mod_role_id") or settings.mod_role_id

        view = IncidentView(
            payload=IncidentViewPayload(
                draft_message=existing_payload.get("draft_message", ""),
                reply_targets=existing_payload.get("reply_targets", []),
                draft_replies=existing_payload.get("draft_replies", []),
                memory_suggestions=existing_payload.get("memory_suggestions", {}),
                mod_role_id=mod_role_id,
                participants=existing_payload.get("participants", []),
                evidence_quotes=existing_payload.get("evidence_quotes", []),
                recommendations=existing_payload.get("recommendations", []),
                rule_ids=existing_payload.get("rule_ids", []),
                source_channel_id=source_channel_id,
                allow_post=bool(existing_payload.get("allow_post", True)),
                allow_actions=bool(existing_payload.get("allow_actions", True)),
                anchor_message_id=int(anchor_id),
                handled=was_handled,
            ),
            memory_store=bot.memory_store,
            view_store=bot.view_store,
        )

        source_parent = (
            source_channel.parent if isinstance(source_channel, discord.Thread) else source_channel
        )
        role_names = ", ".join(f"@{r.name}" for r in anchor.role_mentions) or "the modmail bot"
        context = (
            _ping_context_text(anchor, role_names, source_parent.name),
            anchor.jump_url,
        )

        outcome = await bot._refresh_incident_evidence(
            message=brief_message,
            view=view,
            anchor=anchor,
            channel=source_channel,
            guild=source_channel.guild,
            title="Auto Mod Brief",
            context=context,
            mod_role_id=mod_role_id,
            guild_id=guild_id,
            ctx=f"refresh_brief {brief_message_id}",
            dry_run=not apply,
            force=force,
        )
        if outcome is None:
            print("Nothing new since the last analysis - nothing to refresh.")
            return
        new_embed, result = outcome

        print(_render(new_embed, label="what the refresh produces"))

        if not apply:
            print("\n(dry run - pass --apply to edit the live message)", file=sys.stderr)
        else:
            print("\nEDIT APPLIED", file=sys.stderr)
    finally:
        if logged_in:
            await bot.close()
        # bot.close() doesn't close either store, and an open aiosqlite
        # connection's worker thread keeps the process alive after
        # asyncio.run returns - every run of this tool used to hang forever.
        await bot.memory_store.close()
        await bot.view_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("target", help="link to the Mod bot's own brief message")
    parser.add_argument(
        "--apply", action="store_true", help="edit the live message instead of only printing"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="recompute even if this look finds nothing new - for re-rendering "
        "a brief against evidence already known but not yet reflected on the "
        "card, e.g. after a code change",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.target, apply=args.apply, force=args.force))
    except discord.HTTPException as exc:
        print(f"Discord API error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
