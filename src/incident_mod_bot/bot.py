from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import discord
import httpx
from discord import app_commands
from dotenv import load_dotenv
from openai import AuthenticationError

from incident_mod_bot.config import Settings, load_settings
from incident_mod_bot.discord_ui.incident_view import (
    _ACTION_FIELD,
    _AUDIT_LOOKBACK_BEFORE_S,
    _AUDIT_FOLLOW_UP_S,
    IncidentView,
    IncidentViewPayload,
    _snowflake_created_at,
    merge_new_lines,
)
from incident_mod_bot.discord_ui.view_store import ViewRecord, ViewStore
from incident_mod_bot.memory.store import MemoryStore, format_enforcement_report
from incident_mod_bot.openai_client import (
    OpenAISettings,
    analyze_incident,
    create_client,
    refine_incident_with_images,
    summarize_images,
    summarize_rules,
)
from incident_mod_bot.pipeline.incident import (
    IncidentResult,
    ReplyTarget,
    known_users_from_payload,
    parse_incident_result,
)
from incident_mod_bot.pipeline.ping_context import (
    PING_HISTORY_WINDOW_S,
    analysis_image_candidates,
    build_ping_history,
    format_ping_history,
    reported_message_pointer,
)
from incident_mod_bot.utils.discord import display_name, is_mod
from incident_mod_bot.utils.images import resize_image_bytes, to_data_url
from incident_mod_bot.utils.text import compress_text, human_timedelta, truncate

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("incident_mod_bot")

DEFAULT_AUTO_IGNORE_CATEGORY_NAMES = {"Moderation", "Logs", "Modmail", "Information"}

# A voice channel carries a text chat too, and a ping typed there deserves the
# same handling as one typed anywhere else - someone asking for a mod doesn't
# care that the channel also happens to have a waveform in it.
_AUTO_MOD_SOURCE_TYPES = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)

_MENTION_RE = re.compile(r"<@[&!]?\d+>")

_BARE_PING_FOLLOWUP_SCAN_LIMIT = 30


def _is_bare_ping(message: discord.Message) -> bool:
    """Whether a ping message carries no explanation of its own."""
    return not _MENTION_RE.sub("", message.content).strip()


def _ping_reporter(ping: discord.Message) -> dict[str, Any]:
    """Who asked for a moderator, for the analysis payload.

    Without it the model has to infer the reporter from the anchor id, and
    didn't: reporter_w split "mods, kill him" across three messages around a
    bare ping, the follow-up pass picked up "kill him", and the brief turned
    into a violent threat by the reporter instead of the account reported.

    A ping sent as a reply also records what it replied to, from the copy
    Discord attaches to the ping, so the target is known even when it is
    older than the scanned window.
    """
    reporter: dict[str, Any] = {
        "user_id": ping.author.id,
        "name": display_name(ping.author),
        "bare_ping": _is_bare_ping(ping),
    }
    target = _reply_target(ping)
    if target:
        reporter["replied_to"] = target
    return reporter


def _reply_target(message: Any) -> dict[str, Any] | None:
    """The message this one is a Discord reply to: id always, author and
    text when Discord resolved it. None for a forward or a plain message."""
    ref = getattr(message, "reference", None)
    if ref is None or getattr(message, "type", None) != discord.MessageType.reply:
        return None
    ref_id = getattr(ref, "message_id", None)
    if not isinstance(ref_id, int):
        return None
    out: dict[str, Any] = {"id": ref_id}
    resolved = getattr(ref, "resolved", None)
    # A deleted target resolves to DeletedReferencedMessage, which has no author.
    if getattr(resolved, "author", None) is not None:
        out["author_id"] = resolved.author.id
        out["author_name"] = display_name(resolved.author)
        out["content"] = compress_text(resolved.clean_content, max_len=300)
    return out


def _resolved_reply(message: Any) -> list[discord.Message]:
    """The live message this one replies to, when Discord sent it along."""
    ref = getattr(message, "reference", None)
    resolved = getattr(ref, "resolved", None) if ref is not None else None
    return [resolved] if getattr(resolved, "author", None) is not None else []


def _with_likely_reported(
    reporter: dict[str, Any], messages: list[dict[str, Any]], anchor_message_id: int | None
) -> dict[str, Any]:
    """Point the model at the message the ping is about. See
    ping_context.reported_message_pointer: the reply target for a reply-ping,
    else for a bare ping the last message before it from someone else."""
    return reported_message_pointer(reporter, messages, anchor_message_id)


def _ping_context_text(ping: discord.Message, role_names: str, channel_name: str) -> str:
    """The card title for an auto-ping brief. Names whose message the ping
    replied to, so the card says what was flagged whatever the model writes."""
    text = f"{display_name(ping.author)} pinged {role_names} in #{channel_name}"
    target = _reply_target(ping)
    if target and target.get("author_name") and target.get("author_id") != ping.author.id:
        text += f", replying to {target['author_name']}"
    return text


def _days_between(later: datetime, earlier: datetime) -> float | int:
    days = (later - earlier).total_seconds() / 86400
    return round(days, 1) if days < 30 else int(days)


def _author_facts(
    messages: list[Any],
    precompressed: list[dict[str, Any]],
    *,
    anchor_message_id: int | None,
    mod_role_id: int | None,
    prior: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-author facts the message text can't show, keyed by author id.

    spam_account's "Hi wyd guys I'm bored ... Dms open" was read as an off-topic
    post; the account was 22 hours old, which is the tell that makes it a
    scam bot. Measured at the anchor, not now, so a replay or a later
    refresh sees the same numbers the live brief did.

    Account age comes from the author id itself, so every author gets it,
    including ones only known from a persisted payload. Join date, mod
    status and Discord's own spammer flag need the live member and come
    from `prior` (the persisted payload's own map) for everyone else.
    """
    ref = (
        _snowflake_created_at(anchor_message_id)
        if anchor_message_id is not None
        else discord.utils.utcnow()
    )
    facts = {str(k): dict(v) for k, v in (prior or {}).items()}
    for message in messages:
        author = message.author
        if getattr(author, "bot", False):
            continue
        entry = facts.setdefault(str(author.id), {})
        entry["account_age_days"] = _days_between(ref, author.created_at)
        joined_at = getattr(author, "joined_at", None)
        if joined_at is not None:
            entry["joined_server_days"] = _days_between(ref, joined_at)
        flags = getattr(author, "public_flags", None)
        if flags is not None and getattr(flags, "spammer", False):
            entry["discord_spammer_flag"] = True
        if isinstance(author, discord.Member) and is_mod(author, mod_role_id):
            entry["mod"] = True
    for data in precompressed:
        author_id = data.get("author_id")
        if isinstance(author_id, int):
            facts.setdefault(str(author_id), {}).setdefault(
                "account_age_days", _days_between(ref, _snowflake_created_at(author_id))
            )
    return facts


def _consistent_author_names(
    messages: list[dict[str, Any]], preferred: dict[Any, str] | None = None
) -> list[dict[str, Any]]:
    """One name per author id: the preferred one when given, else the one on
    their latest message.

    display_name() gives a server nickname when Discord hands back a Member
    and the username when it hands back a User, so one person can show up
    under two names in the same window. user_s pinged "^" right after telling
    the cosplay spammer "nobody wants that shit here" as "user_s_full", and the
    model read the two as different people. The same split left the
    reporter named by nickname in payload.reporter and by username on every
    one of their messages, so preferred carries the nickname moderators see.
    """
    latest: dict[Any, str] = {}
    for m in messages:
        if m.get("author_id") is not None and m.get("author_name"):
            latest[m["author_id"]] = m["author_name"]
    for author_id, name in (preferred or {}).items():
        if author_id in latest and name:
            latest[author_id] = name
    return [
        {**m, "author_name": latest[m["author_id"]]}
        if m.get("author_id") in latest and m.get("author_name") != latest[m["author_id"]]
        else m
        for m in messages
    ]


def _with_relative_times(
    messages: list[dict[str, Any]], anchor_message_id: int | None
) -> list[dict[str, Any]]:
    """Add t: seconds from the anchor (negative before it), from the ids
    alone. Without it the model can't tell a reply from ten seconds ago
    from one an hour ago."""
    if anchor_message_id is None:
        return messages
    anchor_ms = anchor_message_id >> 22
    return [
        {**m, "t": round(((m["id"] >> 22) - anchor_ms) / 1000)}
        if isinstance(m.get("id"), int)
        else m
        for m in messages
    ]


class _CompressedAuthorView:
    """Duck-types enough of discord.abc.User for display_name() to work."""

    def __init__(self, id_: int | None, name: str) -> None:
        self.id = id_
        self.name = name


class _CompressedMessageView:
    """Adapts an already-persisted compressed message dict (see
    Bot._compress_messages) to the handful of attributes _postprocess_result
    needs, so evidence citing a message that predates the current process -
    or that's since been deleted - still validates instead of getting
    silently dropped as an unknown id.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self.id = data.get("id")
        self.clean_content = data.get("content") or ""
        self.author = _CompressedAuthorView(data.get("author_id"), data.get("author_name") or "")


def forwarded_content(message: Any) -> str:
    """Text carried inside a forwarded message.

    Discord keeps a forward's text, attachments and embeds in
    message_snapshots and leaves the outer content empty, so anything reading
    message.content alone sees a blank message from a user. A scam that was
    forwarded therefore reaches the model as an empty string.
    """
    parts: list[str] = []
    for snapshot in getattr(message, "message_snapshots", None) or []:
        text = (getattr(snapshot, "content", "") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def media_carriers(message: Any) -> list[Any]:
    """The message plus any forwarded snapshots, each of which holds its own
    attachments, embeds and stickers."""
    return [message] + list(getattr(message, "message_snapshots", None) or [])


class GuildScopedCommandTree(app_commands.CommandTree):
    """Refuses commands from servers the bot is only meant to read."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        settings = getattr(interaction.client, "settings", None)
        guild_id = interaction.guild.id if interaction.guild else None
        if settings is not None and not settings.is_active_guild(guild_id):
            logger.info("Ignoring command from an inactive guild guild_id=%s", guild_id)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "This bot does not run commands in this server.", ephemeral=True
                )
            return False
        return True


class IncidentBot(discord.Client):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        super().__init__(intents=intents)
        self.settings = settings
        self.tree = GuildScopedCommandTree(self)
        self.memory_store = MemoryStore(settings.db_path)
        self.view_store = ViewStore(settings.db_path)
        self.openai = create_client(settings.openai_api_key)
        self._auto_last_run: dict[tuple[int, int], float] = {}

    def _ctx(self, interaction: discord.Interaction) -> str:
        guild = interaction.guild
        if guild:
            guild_part = f"{guild.id}({guild.name!r})"
        else:
            guild_part = "(no_guild)"

        channel = interaction.channel
        channel_id = getattr(channel, "id", None)
        channel_name = getattr(channel, "name", None)
        if channel_id is None:
            channel_part = "(no_channel)"
        elif channel_name:
            channel_part = f"{channel_id}({channel_name!r})"
        else:
            channel_part = str(channel_id)

        user = interaction.user
        user_id = getattr(user, "id", None)
        user_name = None
        if isinstance(user, discord.Member):
            user_name = user.display_name
        else:
            user_name = getattr(user, "name", None) or str(user)
        if user_id is None:
            user_part = "(no_user)"
        else:
            user_part = f"{user_id}({user_name!r})"

        return f"guild={guild_part} channel={channel_part} user={user_part}"

    def _log_cmd(self, interaction: discord.Interaction, name: str, **fields: object) -> None:
        parts: list[str] = []
        for key, value in fields.items():
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            parts.append(f"{key}={text}")
        detail = " ".join(parts)
        if detail:
            logger.info("CMD /%s %s %s", name, self._ctx(interaction), detail)
        else:
            logger.info("CMD /%s %s", name, self._ctx(interaction))

    def _dlog(self, interaction: discord.Interaction, message: str, *args: object) -> None:
        if not self.settings.debug_logs:
            return
        logger.info("DEBUG %s " + message, self._ctx(interaction), *args)

    def _dlog_ctx(self, ctx: str, message: str, *args: object) -> None:
        if not self.settings.debug_logs:
            return
        logger.info("DEBUG %s " + message, ctx, *args)

    @staticmethod
    def _ascii_only(text: str) -> str:
        if not text:
            return ""
        return text.encode("ascii", "ignore").decode("ascii")

    @classmethod
    def _sanitize_draft_text(cls, text: str) -> str:
        s = cls._ascii_only(text)
        # Strip Discord emoji markup that renders as emojis.
        # - Custom emoji: <:name:123> / <a:name:123>
        # - Unicode emoji aliases: :smile:
        s = re.sub(r"<a?:[^:>]{2,}:[0-9]+>", "", s)
        s = re.sub(r":[a-z0-9_+\-]{2,}:", "", s, flags=re.IGNORECASE)
        # Avoid accidental mentions; the UI adds pings.
        s = s.replace("@", "")
        s = "\n".join(" ".join(line.split()) for line in s.splitlines())
        return s.strip()

    def _postprocess_result(self, result: IncidentResult, messages: list[Any]) -> None:
        # messages here may mix real discord.Message objects with
        # _CompressedMessageView stand-ins for older content a recompute
        # pulled from storage rather than the live channel - both duck-type
        # the same handful of attributes this validation needs.
        # Sanitize model text.
        result.draft_message = self._sanitize_draft_text(result.draft_message)
        for line in result.draft_replies:
            line.text = self._sanitize_draft_text(line.text)

        valid_user_ids: set[int] = {m.author.id for m in messages}
        valid_message_ids: set[int] = {m.id for m in messages}
        name_to_user_id: dict[str, int] = {}
        for m in messages:
            n = display_name(m.author).strip().lower()
            if not n:
                continue
            # First seen wins; good enough for a local window.
            name_to_user_id.setdefault(n, m.author.id)

        # Fix or drop participants that reference unknown users.
        fixed_participants: list[Any] = []
        seen_uids: set[int] = set()
        for p in result.participants:
            uid = p.user_id
            if uid not in valid_user_ids:
                mapped = name_to_user_id.get((p.name or "").strip().lower())
                if mapped is None:
                    continue
                uid = mapped
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            p.user_id = uid
            fixed_participants.append(p)
        result.participants = fixed_participants

        # Drop per-user draft lines that reference unknown users.
        if result.draft_replies:
            result.draft_replies = [d for d in result.draft_replies if d.user_id in valid_user_ids]

        # Fix or drop evidence quotes that reference unknown messages.
        if result.evidence_quotes:
            by_id: dict[int, discord.Message] = {m.id: m for m in messages}

            def _match_quote_to_message_id(q: str) -> int | None:
                qq = (q or "").strip().lower()
                if not qq:
                    return None
                matches: list[int] = []
                for mid, msg in by_id.items():
                    txt = (msg.clean_content or "").strip().lower()
                    if not txt:
                        continue
                    if qq in txt or txt in qq:
                        matches.append(mid)
                if len(matches) == 1:
                    return matches[0]
                return None

            fixed_quotes: list[Any] = []
            for q in result.evidence_quotes:
                mid = q.message_id
                if mid is not None and mid not in valid_message_ids:
                    mid = _match_quote_to_message_id(q.quote)
                q.message_id = mid if (mid is None or mid in valid_message_ids) else None
                fixed_quotes.append(q)
            result.evidence_quotes = fixed_quotes

        # Drop reply targets that reference unknown users; clear invalid message_id.
        if result.reply_targets:
            fixed_targets: list[ReplyTarget] = []
            for t in result.reply_targets:
                if t.user_id not in valid_user_ids:
                    continue
                if t.message_id is not None and t.message_id not in valid_message_ids:
                    t.message_id = None
                fixed_targets.append(t)
            result.reply_targets = fixed_targets

        # If the model wrote per-user drafts but forgot targets, infer targets from those.
        if not result.reply_targets and result.draft_replies:
            seen: set[int] = set()
            for line in result.draft_replies:
                if line.user_id in seen:
                    continue
                seen.add(line.user_id)
                result.reply_targets.append(ReplyTarget(user_id=line.user_id, message_id=None))

        # If the model wrote a draft but forgot reply_targets, infer from evidence quote authors.
        if not result.reply_targets and result.draft_message:
            by_id: dict[int, discord.Message] = {m.id: m for m in messages}
            user_ids: list[int] = []
            seen_uids: set[int] = set()
            for q in result.evidence_quotes:
                if q.message_id is None:
                    continue
                msg = by_id.get(q.message_id)
                if msg is None:
                    continue
                uid = msg.author.id
                if uid in seen_uids:
                    continue
                seen_uids.add(uid)
                user_ids.append(uid)
                if len(user_ids) >= 3:
                    break
            if user_ids:
                if len(user_ids) == 1:
                    uid = user_ids[0]
                    message_id = None
                    # Prefer replying to the first evidence quote from that user.
                    for q in result.evidence_quotes:
                        if q.message_id is None:
                            continue
                        msg = by_id.get(q.message_id)
                        if msg and msg.author.id == uid:
                            message_id = msg.id
                            break
                    result.reply_targets = [ReplyTarget(user_id=uid, message_id=message_id)]
                else:
                    result.reply_targets = [ReplyTarget(user_id=uid, message_id=None) for uid in user_ids]

        # Ensure single-target replies have a message_id.
        if len(result.reply_targets) == 1 and (
            result.reply_targets[0].message_id is None
            or result.reply_targets[0].message_id not in valid_message_ids
        ):
            uid = result.reply_targets[0].user_id
            for msg in reversed(messages):
                if msg.author.id == uid:
                    result.reply_targets[0].message_id = msg.id
                    break

        # Drop user memory suggestions that reference unknown users/messages.
        if result.memory_suggestions and result.memory_suggestions.user_notes:
            fixed_notes: list[Any] = []
            for note in result.memory_suggestions.user_notes:
                if note.user_id not in valid_user_ids:
                    continue
                if note.evidence_message_id is not None and note.evidence_message_id not in valid_message_ids:
                    note.evidence_message_id = None
                fixed_notes.append(note)
            result.memory_suggestions.user_notes = fixed_notes

        # Avoid doubling the target name when we already prefix with a ping.
        if len(result.reply_targets) == 1 and result.draft_message:
            uid = result.reply_targets[0].user_id
            name = None
            for p in result.participants:
                if p.user_id == uid and p.name:
                    name = p.name.strip()
                    break
            if not name:
                for msg in reversed(messages):
                    if msg.author.id == uid:
                        name = display_name(msg.author)
                        break
            if name:
                dm = result.draft_message
                if dm.lower().startswith(name.lower()):
                    dm = dm[len(name) :].lstrip(" \t\r\n,.:;-\"")
                    result.draft_message = dm

    async def on_ready(self) -> None:
        logger.info("Logged in as %s", self.user)
        if self.settings.debug_logs:
            logger.info("Debug logs enabled")
            logger.info("DB path: %s", self.settings.db_path)
            logger.info(
                "OpenAI model=%s image_detail=%s max_image_dim=%s",
                self.settings.openai_model,
                self.settings.openai_image_detail,
                self.settings.openai_max_image_dim,
            )
        if self.settings.auto_mod_default_channel_id:
            logger.info("Auto mod enabled default_channel_id=%s", self.settings.auto_mod_default_channel_id)
        else:
            logger.info("Auto mod disabled (AUTO_MOD_DEFAULT_CHANNEL_ID not set)")
        await self.memory_store.connect()
        await self.view_store.connect()
        await self._register_commands()
        await self._restore_views()

    async def on_message(self, message: discord.Message) -> None:
        if not self.settings.auto_mod_default_channel_id:
            return
        if message.author.bot:
            return
        if not message.guild:
            return
        if not self.settings.is_active_guild(message.guild.id):
            return
        # Pinging the modmail bot's own account directly is the same ask as
        # pinging the mod role - someone needs a mod and reached for the
        # wrong target. Treated as the same kind of trigger.
        mentions_modmail_bot = any(
            u.id in self.settings.modmail_bot_user_ids for u in message.mentions
        )
        if not message.role_mentions and not mentions_modmail_bot:
            return
        if not isinstance(message.channel, _AUTO_MOD_SOURCE_TYPES + (discord.Thread,)):
            return

        source_parent = message.channel.parent if isinstance(message.channel, discord.Thread) else message.channel
        if isinstance(source_parent, _AUTO_MOD_SOURCE_TYPES):
            if source_parent.name.endswith("-news"):
                return
            category = source_parent.category
            if category and category.name in DEFAULT_AUTO_IGNORE_CATEGORY_NAMES:
                return
        else:
            return

        # Non-mod only.
        try:
            config = await self.memory_store.get_guild_config(message.guild.id)
        except RuntimeError:
            return
        mod_role_id = config.get("mod_role_id") or self.settings.mod_role_id
        member = message.author if isinstance(message.author, discord.Member) else None
        if member is None:
            try:
                member = await message.guild.fetch_member(message.author.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if is_mod(member, mod_role_id):
            return

        asyncio.create_task(self._handle_auto_mod_ping(message, mod_role_id=mod_role_id))

    async def _handle_auto_mod_ping(self, message: discord.Message, *, mod_role_id: int | None) -> None:
        guild = message.guild
        if not guild:
            return
        if not self.settings.auto_mod_default_channel_id:
            return
        channel = message.channel
        if not isinstance(channel, _AUTO_MOD_SOURCE_TYPES + (discord.Thread,)):
            return

        source_parent = channel.parent if isinstance(channel, discord.Thread) else channel
        if not isinstance(source_parent, _AUTO_MOD_SOURCE_TYPES):
            return

        try:
            auto_cfg = await self.memory_store.get_auto_mod_config(guild.id)
            exempt_raw = auto_cfg.get("exempt_suffix")
            exempt_suffix = str(exempt_raw) if isinstance(exempt_raw, str) and exempt_raw else "-news"
            cooldown_raw = auto_cfg.get("cooldown_s")
            if cooldown_raw is None:
                cooldown_s = 180
            elif isinstance(cooldown_raw, (int, str)):
                try:
                    cooldown_s = int(cooldown_raw)
                except ValueError:
                    cooldown_s = 180
            else:
                cooldown_s = 180
            ignored_category_ids = set(await self.memory_store.list_auto_mod_ignored_categories(guild.id))
            routes = await self.memory_store.list_auto_mod_routes(guild.id)
        except RuntimeError:
            return
        except Exception:
            logger.exception("Auto mod config load failed")
            return

        if source_parent.name.endswith(exempt_suffix):
            return
        category = source_parent.category
        if category and (category.id in ignored_category_ids or category.name in DEFAULT_AUTO_IGNORE_CATEGORY_NAMES):
            return

        route_map = {role_id: channel_id for role_id, channel_id in routes}
        mod_channel_ids = set(route_map.values())
        mod_channel_ids.add(self.settings.auto_mod_default_channel_id)
        if source_parent.id in mod_channel_ids:
            return

        key = (guild.id, source_parent.id)
        now_mono = time.monotonic()
        last = self._auto_last_run.get(key)
        if last is not None and now_mono - last < cooldown_s:
            return
        self._auto_last_run[key] = now_mono

        dest_channel_id: int | None = None
        for role in message.role_mentions:
            if role.id in route_map:
                dest_channel_id = route_map[role.id]
                break
        if dest_channel_id is None:
            dest_channel_id = self.settings.auto_mod_default_channel_id

        if not dest_channel_id:
            return
        if dest_channel_id == source_parent.id:
            return

        dest_channel = guild.get_channel(dest_channel_id)
        if not isinstance(dest_channel, discord.TextChannel):
            try:
                fetched = await guild.fetch_channel(dest_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                fetched = None
            dest_channel = fetched if isinstance(fetched, discord.TextChannel) else None
        if not isinstance(dest_channel, discord.TextChannel):
            logger.info(
                "Auto mod dest channel not found guild=%s channel_id=%s",
                guild.id,
                dest_channel_id,
            )
            return

        logger.info(
            "AUTO /mod trigger guild=%s(%r) src=%s(%r) user=%s(%r) roles=%s dest=%s(%r) ping=%s",
            guild.id,
            guild.name,
            source_parent.id,
            source_parent.name,
            message.author.id,
            display_name(message.author),
            ",".join(f"{r.id}(@{r.name})" for r in message.role_mentions) or "(modmail bot mention)",
            dest_channel.id,
            dest_channel.name,
            message.id,
        )

        # Fetch context ending at the ping.
        max_limit = self.settings.max_limit
        use_limit = min(max(self.settings.default_limit, 1), max_limit)
        try:
            messages = await self._fetch_recent_messages_ending_at(
                channel, limit=use_limit, end_message=message
            )
        except (discord.Forbidden, discord.HTTPException):
            return
        if not messages:
            return

        now = discord.utils.utcnow()
        oldest = messages[0].created_at
        scan_label = f"last {human_timedelta(now - oldest)}"

        # No role mention means this was a direct ping of the modmail bot's
        # own account - still worth naming what was actually summoned.
        role_names = ", ".join(f"@{r.name}" for r in message.role_mentions) or "the modmail bot"
        ctx = (
            f"guild={guild.id}({guild.name!r}) "
            f"channel={getattr(channel, 'id', None)}({getattr(channel, 'name', None)!r}) "
            f"user={message.author.id}({display_name(message.author)!r})"
        )
        try:
            result, raw_result, analysis_payload = await self._analyze_incident_messages(
                guild_id=guild.id,
                messages=messages,
                mod_role_id=mod_role_id,
                anchor_message_id=message.id,
                ctx=ctx,
                reporter=_ping_reporter(message),
                extra_messages=_resolved_reply(message),
            )
        except AuthenticationError:
            logger.exception("OpenAI auth error during auto /mod")
            return
        except Exception:
            logger.exception("Auto /mod failed")
            return

        # The verb carries the link. A trailing "(jump)" is a second thing to read
        # for the same destination.
        context = (
            _ping_context_text(message, role_names, source_parent.name),
            message.jump_url,
        )
        embed = self._build_incident_embed(
            result,
            title="Auto Mod Brief",
            scan_label=scan_label,
            context=context,
        )

        action_participants: list[dict[str, Any]] = []
        seen_users: set[int] = set()
        for msg in messages:
            user_id = msg.author.id
            if user_id in seen_users:
                continue
            seen_users.add(user_id)
            m = msg.author if isinstance(msg.author, discord.Member) else None
            action_participants.append(
                {
                    "user_id": user_id,
                    "name": display_name(msg.author),
                    "role": "mod" if is_mod(m, mod_role_id) else "member",
                }
            )
            if len(action_participants) >= 25:
                break

        view_payload = IncidentViewPayload(
            draft_message=result.draft_message,
            reply_targets=[t.model_dump() for t in result.reply_targets],
            draft_replies=[r.model_dump() for r in result.draft_replies],
            memory_suggestions=result.memory_suggestions.model_dump(),
            mod_role_id=mod_role_id,
            participants=action_participants,
            evidence_quotes=[q.model_dump() for q in result.evidence_quotes],
            recommendations=list(result.recommendations or []),
            rule_ids=[r.id for r in (result.rule_refs or [])],
            source_channel_id=channel.id,
            allow_post=True,
            allow_actions=True,
            anchor_message_id=message.id,
            handled=False,
        )
        view = IncidentView(
            payload=view_payload,
            memory_store=self.memory_store,
            view_store=self.view_store,
        )
        try:
            posted = await dest_channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
                silent=True,
            )
        except (discord.Forbidden, discord.HTTPException):
            return
        logger.info(
            "BRIEF posted via=%s msg=%s channel=%s headline=%r participants=%d "
            "rules=%s recommendations=%d confidence=%.2f reply_to=%s draft=%r",
            "automod",
            posted.id,
            getattr(getattr(posted, "channel", None), "id", None),
            (getattr(result, "headline", "") or "")[:80],
            len(result.participants or []),
            ",".join(r.id for r in (result.rule_refs or [])) or "-",
            len(result.recommendations or []),
            float(getattr(result, "confidence", 0.0) or 0.0),
            # who the draft is aimed at. The background image pass overwrites
            # the stored view, so without this there is no record of what the
            # moderator actually saw before refinement landed.
            ",".join(str(getattr(t, "user_id", t)) for t in (result.reply_targets or [])) or "-",
            (getattr(result, "draft_message", "") or "")[:100],
        )
        record = ViewRecord(
            message_id=posted.id,
            channel_id=posted.channel.id,
            guild_id=posted.guild.id if posted.guild else 0,
            payload=view_payload.to_dict(),
            created_at=time.time(),
        )
        await self.view_store.save_view(record)
        try:
            await self.memory_store.save_incident_payload(
                posted.id, guild.id, {**analysis_payload, "source_channel_id": channel.id}
            )
        except Exception:
            logger.exception("Failed to persist incident payload for replay")

        asyncio.create_task(
            self._maybe_update_brief_with_images(
                message=posted,
                view=view,
                base_result=result,
                base_raw_result=raw_result,
                messages=messages,
                scan_label=scan_label,
                title="Auto Mod Brief",
                context=context,
                action_participants=action_participants,
                mod_role_id=mod_role_id,
                persist_view=True,
                ctx=ctx,
            )
        )

        # Every auto-triggered brief gets this, not just bare pings: the
        # audit-log half in particular can vindicate or contradict any
        # brief, and the only way to find out is to look.
        asyncio.create_task(
            self._maybe_update_brief_with_followup(
                message=posted,
                view=view,
                anchor=message,
                title="Auto Mod Brief",
                context=context,
                mod_role_id=mod_role_id,
                guild_id=guild.id,
                ctx=ctx,
            )
        )

    async def _register_commands(self) -> None:
        # on_ready can fire multiple times on reconnect; keep this idempotent.
        self.tree.clear_commands(guild=None)
        self.tree.add_command(
            app_commands.Command(
                name="mod",
                description="Analyze recent messages and suggest moderation steps.",
                callback=self._mod_command,
            )
        )
        self.tree.add_command(
            app_commands.ContextMenu(
                name="Mod",
                callback=self._mod_message_context,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="incident_config",
                description="Configure mod assistant rules channel or mod role.",
                callback=self._mod_config,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="incident_rules_sync",
                description="Sync and summarize configured rules channel.",
                callback=self._mod_rules_sync,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="incident_memory_add",
                description="Add a server memory note.",
                callback=self._mod_memory_add,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="incident_memory_list",
                description="List recent server memory notes.",
                callback=self._mod_memory_list,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="enforcement",
                description="How this server has actually enforced its rules.",
                callback=self._mod_enforcement,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="incident_memory_reset",
                description="Clear server and user memory.",
                callback=self._mod_memory_reset,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="incident_auto_route_set",
                description="Route role pings to a mod channel.",
                callback=self._incident_auto_route_set,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="incident_auto_route_clear",
                description="Remove a role ping route.",
                callback=self._incident_auto_route_clear,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="incident_auto_route_list",
                description="List role ping routes.",
                callback=self._incident_auto_route_list,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="incident_auto_ignore_add",
                description="Ignore pings in a category.",
                callback=self._incident_auto_ignore_add,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="incident_auto_ignore_remove",
                description="Stop ignoring pings in a category.",
                callback=self._incident_auto_ignore_remove,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="incident_auto_ignore_list",
                description="List ignored categories for auto /mod.",
                callback=self._incident_auto_ignore_list,
            )
        )
        try:
            synced = await self.tree.sync()
        except Exception:
            logger.exception("Slash command sync failed")
            return
        logger.info(
            "Synced %s global app commands: %s",
            len(synced),
            ", ".join(cmd.name for cmd in synced),
        )

    async def _mod_config(
        self,
        interaction: discord.Interaction,
        rules_channel: discord.TextChannel | None = None,
        mod_role: discord.Role | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        self._log_cmd(
            interaction,
            "incident_config",
            rules_channel_id=rules_channel.id if rules_channel else None,
            mod_role_id=mod_role.id if mod_role else None,
        )
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not is_mod(member, self.settings.mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return
        await self.memory_store.set_guild_config(
            interaction.guild.id,
            rules_channel_id=rules_channel.id if rules_channel else None,
            mod_role_id=mod_role.id if mod_role else None,
        )
        new_config = await self.memory_store.get_guild_config(interaction.guild.id)
        self._dlog(
            interaction,
            "Config now rules_channel_id=%s mod_role_id=%s",
            new_config.get("rules_channel_id"),
            new_config.get("mod_role_id"),
        )
        await interaction.response.send_message("Config saved.", ephemeral=True)

    async def _mod_rules_sync(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        self._log_cmd(interaction, "incident_rules_sync")
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not is_mod(member, self.settings.mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return
        config = await self.memory_store.get_guild_config(interaction.guild.id)
        rules_channel_id = config.get("rules_channel_id")
        if not rules_channel_id:
            await interaction.response.send_message("Rules channel not configured.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.guild.get_channel(rules_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("Rules channel not found.", ephemeral=True)
            return
        t0 = time.monotonic()
        rules_text, scanned, kept = await self._fetch_all_text(channel)
        self._dlog(
            interaction,
            "Rules fetch scanned=%s kept=%s chars=%s in %.2fs",
            scanned,
            kept,
            len(rules_text),
            time.monotonic() - t0,
        )
        if not rules_text.strip():
            await interaction.followup.send("Rules channel has no text to summarize.", ephemeral=True)
            return
        openai_settings = OpenAISettings(
            api_key=self.settings.openai_api_key,
            model=self.settings.openai_model,
            image_detail=self.settings.openai_image_detail,
            debug_logs=self.settings.debug_logs,
        )
        try:
            self._dlog(
                interaction,
                "OpenAI summarize_rules model=%s chars=%s",
                openai_settings.model,
                len(rules_text),
            )
            summary = await asyncio.to_thread(summarize_rules, self.openai, openai_settings, rules_text)
        except AuthenticationError as exc:
            logger.exception("OpenAI auth error during rules sync")
            await interaction.followup.send(
                "OpenAI auth error while summarizing rules. "
                "If you are using a restricted key, enable the `model.request` scope (and `responses`). "
                f"Details: {exc}",
                ephemeral=True,
            )
            return
        except Exception as exc:
            logger.exception("Rules summary failed")
            if self.settings.debug_logs:
                await interaction.followup.send(
                    truncate(f"Failed to summarize rules: {exc}", 1800),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send("Failed to summarize rules.", ephemeral=True)
            return
        rule_count = 0
        if isinstance(summary, dict):
            rules = summary.get("rules")
            if isinstance(rules, list):
                rule_count = len(rules)
        self._dlog(interaction, "Rules summary produced rules=%s", rule_count)
        await self.memory_store.set_rules_memory(interaction.guild.id, json.dumps(summary, ensure_ascii=True))
        self._dlog(interaction, "Rules memory saved")
        await interaction.followup.send("Rules synced.", ephemeral=True)

    async def _mod_memory_add(self, interaction: discord.Interaction, text: str) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        self._log_cmd(interaction, "incident_memory_add", chars=len(text))
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not is_mod(member, self.settings.mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return
        await self.memory_store.add_server_memory(interaction.guild.id, text.strip())
        self._dlog(interaction, "Server memory note saved")
        await interaction.response.send_message("Memory saved.", ephemeral=True)

    async def _mod_memory_list(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        self._log_cmd(interaction, "incident_memory_list")
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not is_mod(member, self.settings.mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return
        notes = await self.memory_store.list_server_memory(interaction.guild.id, limit=10)
        self._dlog(interaction, "Server memory notes=%s", len(notes))
        if not notes:
            await interaction.response.send_message("No memory notes saved.", ephemeral=True)
            return
        formatted = "\n".join(f"- {note}" for note in notes)
        await interaction.response.send_message(formatted, ephemeral=True)

    async def _mod_enforcement(self, interaction: discord.Interaction) -> None:
        """Show the enforcement ledger: norms, activity, and where advice diverges."""
        if not interaction.guild:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        self._log_cmd(interaction, "enforcement")
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not is_mod(member, self.settings.mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return
        guild_id = interaction.guild.id
        try:
            stats = await self.memory_store.enforcement_stats(guild_id)
            norms = await self.memory_store.summarize_enforcement(guild_id)
            divergence = await self.memory_store.enforcement_divergence(guild_id)
        except Exception:
            logger.exception("failed to build enforcement report")
            await interaction.response.send_message(
                "Could not read enforcement history.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            format_enforcement_report(norms=norms, divergence=divergence, stats=stats),
            ephemeral=True,
        )

    async def _mod_memory_reset(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        self._log_cmd(interaction, "incident_memory_reset")
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not is_mod(member, self.settings.mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return
        await self.memory_store.delete_server_memory(interaction.guild.id)
        await self.memory_store.delete_user_memory(interaction.guild.id)
        self._dlog(interaction, "Server/user memory cleared")
        await interaction.response.send_message("Memory cleared.", ephemeral=True)

    async def _incident_auto_route_set(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        channel: discord.TextChannel,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        self._log_cmd(interaction, "incident_auto_route_set", role_id=role.id, channel_id=channel.id)
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        config = await self.memory_store.get_guild_config(interaction.guild.id)
        mod_role_id = config.get("mod_role_id") or self.settings.mod_role_id
        if not is_mod(member, mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return
        await self.memory_store.set_auto_mod_route(interaction.guild.id, role.id, channel.id)
        await interaction.response.send_message(
            f"Auto mod route set: @{role.name} -> {channel.mention}", ephemeral=True
        )

    async def _incident_auto_route_clear(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        self._log_cmd(interaction, "incident_auto_route_clear", role_id=role.id)
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        config = await self.memory_store.get_guild_config(interaction.guild.id)
        mod_role_id = config.get("mod_role_id") or self.settings.mod_role_id
        if not is_mod(member, mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return
        await self.memory_store.delete_auto_mod_route(interaction.guild.id, role.id)
        await interaction.response.send_message(
            f"Auto mod route cleared for @{role.name}.", ephemeral=True
        )

    async def _incident_auto_route_list(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        self._log_cmd(interaction, "incident_auto_route_list")
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        config = await self.memory_store.get_guild_config(interaction.guild.id)
        mod_role_id = config.get("mod_role_id") or self.settings.mod_role_id
        if not is_mod(member, mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return
        routes = await self.memory_store.list_auto_mod_routes(interaction.guild.id)
        if not routes:
            await interaction.response.send_message("No auto mod routes configured.", ephemeral=True)
            return
        lines: list[str] = []
        for role_id, channel_id in routes:
            role_obj = interaction.guild.get_role(role_id)
            role_label = f"@{role_obj.name}" if role_obj else f"role:{role_id}"
            ch = interaction.guild.get_channel(channel_id)
            channel_label = ch.mention if isinstance(ch, discord.TextChannel) else f"channel:{channel_id}"
            lines.append(f"- {role_label} -> {channel_label}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _incident_auto_ignore_add(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        self._log_cmd(interaction, "incident_auto_ignore_add", category_id=category.id)
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        config = await self.memory_store.get_guild_config(interaction.guild.id)
        mod_role_id = config.get("mod_role_id") or self.settings.mod_role_id
        if not is_mod(member, mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return
        await self.memory_store.add_auto_mod_ignored_category(interaction.guild.id, category.id)
        await interaction.response.send_message(
            f"Auto mod will ignore pings in category: {category.name}", ephemeral=True
        )

    async def _incident_auto_ignore_remove(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        self._log_cmd(interaction, "incident_auto_ignore_remove", category_id=category.id)
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        config = await self.memory_store.get_guild_config(interaction.guild.id)
        mod_role_id = config.get("mod_role_id") or self.settings.mod_role_id
        if not is_mod(member, mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return
        await self.memory_store.delete_auto_mod_ignored_category(interaction.guild.id, category.id)
        await interaction.response.send_message(
            f"Auto mod will no longer ignore category: {category.name}", ephemeral=True
        )

    async def _incident_auto_ignore_list(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        self._log_cmd(interaction, "incident_auto_ignore_list")
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        config = await self.memory_store.get_guild_config(interaction.guild.id)
        mod_role_id = config.get("mod_role_id") or self.settings.mod_role_id
        if not is_mod(member, mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return
        ids = await self.memory_store.list_auto_mod_ignored_categories(interaction.guild.id)
        if not ids:
            await interaction.response.send_message("No ignored categories configured.", ephemeral=True)
            return
        lines: list[str] = []
        for category_id in ids:
            cat = interaction.guild.get_channel(category_id)
            label = cat.name if isinstance(cat, discord.CategoryChannel) else str(category_id)
            lines.append(f"- {label} ({category_id})")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _mod_command(
        self,
        interaction: discord.Interaction,
        limit: int | None = None,
    ) -> None:
        if not interaction.guild or not interaction.channel:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        self._log_cmd(
            interaction,
            "mod",
            limit=limit,
        )
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        config = await self.memory_store.get_guild_config(interaction.guild.id)
        mod_role_id = config.get("mod_role_id") or self.settings.mod_role_id
        self._dlog(interaction, "Resolved mod_role_id=%s", mod_role_id)
        if not is_mod(member, mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        t_cmd = time.monotonic()
        max_limit = self.settings.max_limit
        use_limit = min(max(limit or self.settings.default_limit, 1), max_limit)
        self._dlog(interaction, "Using limit=%s (max=%s)", use_limit, max_limit)
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.edit_original_response(content="Unsupported channel type.")
            return
        t0 = time.monotonic()
        messages = await self._fetch_recent_messages(channel, use_limit)
        self._dlog(interaction, "Fetched messages=%s in %.2fs", len(messages), time.monotonic() - t0)
        if not messages:
            await interaction.edit_original_response(content="No messages found.")
            return

        now = discord.utils.utcnow()
        oldest = messages[0].created_at
        scan_label = f"last {human_timedelta(now - oldest)}"
        try:
            result, raw_result, analysis_payload = await self._analyze_incident_messages(
                guild_id=interaction.guild.id,
                messages=messages,
                mod_role_id=mod_role_id,
                ctx=self._ctx(interaction),
            )
        except AuthenticationError as exc:
            logger.exception("OpenAI auth error during incident analysis")
            await interaction.edit_original_response(
                content=(
                    "OpenAI auth error while running /mod. "
                    "If you are using a restricted key, enable the `model.request` scope. "
                    f"Details: {exc}"
                )
            )
            return
        except Exception as exc:
            logger.exception("Incident analysis failed")
            if self.settings.debug_logs:
                await interaction.edit_original_response(
                    content=truncate(f"Incident analysis failed: {exc}", 1800)
                )
            else:
                await interaction.edit_original_response(content="Incident analysis failed.")
            return
        embed = self._build_incident_embed(result, scan_label=scan_label)

        action_participants: list[dict[str, Any]] = []
        seen_users: set[int] = set()
        for message in messages:
            user_id = message.author.id
            if user_id in seen_users:
                continue
            seen_users.add(user_id)
            member = message.author if isinstance(message.author, discord.Member) else None
            action_participants.append(
                {
                    "user_id": user_id,
                    "name": display_name(message.author),
                    "role": "mod" if is_mod(member, mod_role_id) else "member",
                }
            )
            if len(action_participants) >= 25:
                break
        view_payload = IncidentViewPayload(
            draft_message=result.draft_message,
            reply_targets=[t.model_dump() for t in result.reply_targets],
            draft_replies=[r.model_dump() for r in result.draft_replies],
            memory_suggestions=result.memory_suggestions.model_dump(),
            mod_role_id=mod_role_id,
            participants=action_participants,
            evidence_quotes=[q.model_dump() for q in result.evidence_quotes],
            recommendations=list(result.recommendations or []),
            rule_ids=[r.id for r in (result.rule_refs or [])],
            source_channel_id=channel.id,
            allow_post=True,
            allow_actions=True,
            handled=False,
        )
        view = IncidentView(
            payload=view_payload,
            memory_store=self.memory_store,
            view_store=self.view_store,
        )
        posted = await interaction.edit_original_response(embed=embed, view=view)
        logger.info(
            "BRIEF posted via=%s msg=%s channel=%s headline=%r participants=%d "
            "rules=%s recommendations=%d confidence=%.2f reply_to=%s draft=%r",
            "slash",
            posted.id,
            getattr(getattr(posted, "channel", None), "id", None),
            (getattr(result, "headline", "") or "")[:80],
            len(result.participants or []),
            ",".join(r.id for r in (result.rule_refs or [])) or "-",
            len(result.recommendations or []),
            float(getattr(result, "confidence", 0.0) or 0.0),
            # who the draft is aimed at. The background image pass overwrites
            # the stored view, so without this there is no record of what the
            # moderator actually saw before refinement landed.
            ",".join(str(getattr(t, "user_id", t)) for t in (result.reply_targets or [])) or "-",
            (getattr(result, "draft_message", "") or "")[:100],
        )
        try:
            await self.memory_store.save_incident_payload(
                posted.id,
                interaction.guild.id,
                {**analysis_payload, "source_channel_id": channel.id},
            )
        except Exception:
            logger.exception("Failed to persist incident payload for replay")
        logger.info("BRIEF generated in %.2fs", time.monotonic() - t_cmd)
        self._dlog(interaction, "Completed /mod in %.2fs", time.monotonic() - t_cmd)

        # Background image refinement (only updates if images are actually relevant).
        asyncio.create_task(
            self._maybe_update_brief_with_images(
                message=posted,
                view=view,
                base_result=result,
                base_raw_result=raw_result,
                messages=messages,
                scan_label=scan_label,
                title="Mod Brief",
                context=None,
                action_participants=action_participants,
                mod_role_id=mod_role_id,
                persist_view=False,
                ctx=self._ctx(interaction),
            )
        )

    async def _mod_message_context(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server-only command.", ephemeral=True)
            return
        self._log_cmd(
            interaction,
            "mod_message_context",
            message_id=message.id,
            channel_id=getattr(message.channel, "id", None),
        )
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        config = await self.memory_store.get_guild_config(interaction.guild.id)
        mod_role_id = config.get("mod_role_id") or self.settings.mod_role_id
        if not is_mod(member, mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        channel = message.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.edit_original_response(content="Unsupported channel type.")
            return

        after_limit = 10
        max_total = self.settings.max_limit
        before_limit = min(self.settings.default_limit, max(max_total - after_limit, 1))

        t0 = time.monotonic()
        before_messages = await self._fetch_recent_messages_ending_at(
            channel, limit=before_limit, end_message=message
        )
        if not before_messages or before_messages[-1].id != message.id:
            before_messages.append(message)
        self._dlog(
            interaction,
            "Context fetch before+anchor=%s in %.2fs",
            len(before_messages),
            time.monotonic() - t0,
        )

        t0 = time.monotonic()
        after_messages: list[discord.Message] = []
        async for msg in channel.history(limit=after_limit, after=message, oldest_first=True):
            if msg.author.bot:
                continue
            after_messages.append(msg)
        self._dlog(
            interaction,
            "Context fetch after=%s in %.2fs",
            len(after_messages),
            time.monotonic() - t0,
        )

        window = before_messages + after_messages
        if not window:
            await interaction.edit_original_response(content="No messages found.")
            return

        oldest = window[0].created_at
        latest = window[-1].created_at
        before_count = max(len(before_messages) - 1, 0)
        after_count = len(after_messages)
        scan_label = f"{before_count} before + {after_count} after | {human_timedelta(latest - oldest)} span"
        context = (f"Message flagged in #{channel.name}", message.jump_url)

        try:
            result, raw_result, analysis_payload = await self._analyze_incident_messages(
                guild_id=interaction.guild.id,
                messages=window,
                mod_role_id=mod_role_id,
                anchor_message_id=message.id,
                ctx=self._ctx(interaction),
            )
        except AuthenticationError as exc:
            logger.exception("OpenAI auth error during context menu analysis")
            await interaction.edit_original_response(
                content=(
                    "OpenAI auth error while running Mod (message). "
                    "If you are using a restricted key, enable the `model.request` scope. "
                    f"Details: {exc}"
                )
            )
            return
        except Exception as exc:
            logger.exception("Context menu analysis failed")
            if self.settings.debug_logs:
                await interaction.edit_original_response(
                    content=truncate(f"Context menu analysis failed: {exc}", 1800)
                )
            else:
                await interaction.edit_original_response(content="Context menu analysis failed.")
            return

        embed = self._build_incident_embed(result, scan_label=scan_label, context=context)

        action_participants: list[dict[str, Any]] = []
        seen_users: set[int] = set()
        for msg in window:
            user_id = msg.author.id
            if user_id in seen_users:
                continue
            seen_users.add(user_id)
            m = msg.author if isinstance(msg.author, discord.Member) else None
            action_participants.append(
                {
                    "user_id": user_id,
                    "name": display_name(msg.author),
                    "role": "mod" if is_mod(m, mod_role_id) else "member",
                }
            )
            if len(action_participants) >= 25:
                break

        view_payload = IncidentViewPayload(
            draft_message=result.draft_message,
            reply_targets=[t.model_dump() for t in result.reply_targets],
            draft_replies=[r.model_dump() for r in result.draft_replies],
            memory_suggestions=result.memory_suggestions.model_dump(),
            mod_role_id=mod_role_id,
            participants=action_participants,
            evidence_quotes=[q.model_dump() for q in result.evidence_quotes],
            recommendations=list(result.recommendations or []),
            rule_ids=[r.id for r in (result.rule_refs or [])],
            source_channel_id=channel.id,
            allow_post=True,
            allow_actions=True,
            anchor_message_id=message.id,
            handled=False,
        )
        view = IncidentView(
            payload=view_payload,
            memory_store=self.memory_store,
            view_store=self.view_store,
        )
        posted = await interaction.edit_original_response(embed=embed, view=view)
        try:
            await self.memory_store.save_incident_payload(
                posted.id,
                interaction.guild.id,
                {**analysis_payload, "source_channel_id": channel.id},
            )
        except Exception:
            logger.exception("Failed to persist incident payload for replay")

        asyncio.create_task(
            self._maybe_update_brief_with_images(
                message=posted,
                view=view,
                base_result=result,
                base_raw_result=raw_result,
                messages=window,
                scan_label=scan_label,
                title="Mod Brief",
                context=context,
                action_participants=action_participants,
                mod_role_id=mod_role_id,
                persist_view=False,
                ctx=self._ctx(interaction),
            )
        )

    async def _analyze_incident_messages(
        self,
        *,
        guild_id: int,
        messages: list[discord.Message],
        mod_role_id: int | None,
        anchor_message_id: int | None = None,
        ctx: str,
        precompressed_base: list[dict[str, Any]] | None = None,
        audit_findings: list[str] | None = None,
        reporter: dict[str, Any] | None = None,
        prior_authors: dict[str, dict[str, Any]] | None = None,
        extra_messages: list[discord.Message] | None = None,
        fetch_message: Any = None,
    ) -> tuple[IncidentResult, dict[str, Any], dict[str, Any]]:
        # The few images nearest the ping go in with the text (see
        # _collect_analysis_images); the background pass looks at the rest.
        # extra_messages: live copies of messages outside `messages` (the
        # ping's reply target) whose images may matter. fetch_message: async
        # id -> Message, for images in messages only known from storage.
        rules_task = asyncio.create_task(self.memory_store.get_rules_memory(guild_id))
        server_task = asyncio.create_task(self.memory_store.list_server_memory(guild_id, limit=5))
        # Earned context: how this server has actually enforced its rules.
        # Aggregate only - never names individuals. Empty until the enforcement
        # ledger has data, at which point this switches itself on.
        norms_task = asyncio.create_task(self.memory_store.summarize_enforcement(guild_id))
        user_task = asyncio.create_task(self._collect_user_memory(guild_id, messages))

        openai_settings = OpenAISettings(
            api_key=self.settings.openai_api_key,
            model=self.settings.openai_model,
            image_detail=self.settings.openai_image_detail,
            debug_logs=self.settings.debug_logs,
        )

        rules_memory_raw = await rules_task
        rules_memory: Any = rules_memory_raw or "(rules not configured)"
        if rules_memory_raw:
            try:
                rules_memory = json.loads(rules_memory_raw)
            except json.JSONDecodeError:
                rules_memory = rules_memory_raw
        server_memory = await server_task
        user_memory = await user_task
        try:
            enforcement_norms = await norms_task
        except Exception:
            logger.exception("failed to summarize enforcement history")
            enforcement_norms = []
        if enforcement_norms:
            server_memory = list(server_memory) + [
                "Observed enforcement in this server: " + " ".join(enforcement_norms)
            ]

        preferred_names: dict[Any, str] = {
            m.author.id: display_name(m.author)
            for m in messages
            if isinstance(m.author, discord.Member)
        }
        if reporter and reporter.get("user_id") is not None and reporter.get("name"):
            preferred_names[reporter["user_id"]] = reporter["name"]

        payload = {
            "rules_memory": rules_memory,
            "server_memory": server_memory,
            "user_memory": user_memory,
            "messages": _with_relative_times(
                _consistent_author_names(
                    (precompressed_base or []) + self._compress_messages(messages, set()),
                    preferred_names,
                ),
                anchor_message_id,
            ),
            "authors": _author_facts(
                messages,
                precompressed_base or [],
                anchor_message_id=anchor_message_id,
                mod_role_id=mod_role_id,
                prior=prior_authors,
            ),
        }
        if anchor_message_id is not None:
            payload["anchor_message_id"] = anchor_message_id
        if audit_findings:
            payload["audit_findings"] = audit_findings
        if reporter:
            payload["reporter"] = _with_likely_reported(
                reporter, payload["messages"], anchor_message_id
            )
            if anchor_message_id is not None:
                history = await self._ping_history(
                    guild_id, payload["reporter"], anchor_message_id
                )
                if history:
                    payload["reporter"]["recent_pings"] = history

        live = {m.id: m for m in list(messages) + list(extra_messages or [])}
        images, analysed_media = await self._collect_analysis_images(
            payload, live=live, fetch_message=fetch_message, ctx=ctx
        )

        self._dlog_ctx(
            ctx,
            "Context rules=%s server_memory=%s user_memory=%s images=%s",
            "yes" if rules_memory_raw else "no",
            len(server_memory),
            len(user_memory),
            len(images),
        )

        self._dlog_ctx(ctx, "OpenAI analyze_incident model=%s", openai_settings.model)
        t0 = time.monotonic()

        async def _run_analysis() -> dict[str, Any]:
            client = create_client(self.settings.openai_api_key)
            try:
                return await asyncio.to_thread(
                    analyze_incident, client, openai_settings, payload, images
                )
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        raw_result = await asyncio.create_task(_run_analysis())
        result = parse_incident_result(raw_result, known_users_from_payload(payload))
        # Carry through how much past enforcement shaped this brief, so the
        # ledger's contribution is visible instead of silent.
        result.informed_by = len(enforcement_norms)
        result.ping_history = format_ping_history(
            (payload.get("reporter") or {}).get("recent_pings")
        )
        result.analysed_media = sorted(analysed_media)

        self._postprocess_result(
            result,
            list(messages) + [_CompressedMessageView(d) for d in (precompressed_base or [])],
        )
        self._dlog_ctx(ctx, "analyze_incident parsed in %.2fs", time.monotonic() - t0)

        message_links = {m.id: m.jump_url for m in messages}
        for q in result.evidence_quotes:
            if q.link:
                continue
            if q.message_id and q.message_id in message_links:
                q.link = message_links[q.message_id]
        for note in result.memory_suggestions.user_notes:
            if note.evidence_link:
                continue
            if note.evidence_message_id and note.evidence_message_id in message_links:
                note.evidence_link = message_links[note.evidence_message_id]

        self._dlog_ctx(
            ctx,
            "Result conf=%.2f participants=%s rules=%s recs=%s evidence=%s reply_targets=%s draft_len=%s memory server=%s user=%s",
            result.confidence,
            len(result.participants),
            len(result.rule_refs),
            len(result.recommendations),
            len(result.evidence_quotes),
            len(result.reply_targets),
            len(result.draft_message),
            len(result.memory_suggestions.server_notes),
            len(result.memory_suggestions.user_notes),
        )
        return result, raw_result, payload

    async def _ping_history(
        self, guild_id: int, reporter: dict[str, Any], anchor_message_id: int
    ) -> dict[str, Any] | None:
        """The reporter's earlier mod pings, from stored briefs. One person
        pinged six times in four days, each reply-ping aimed at someone
        taunting them, and every brief judged the ping on its own."""
        user_id = reporter.get("user_id")
        if not isinstance(user_id, int):
            return None
        try:
            earlier, outcomes = await self.memory_store.list_earlier_pings(
                guild_id,
                user_id,
                before_message_id=anchor_message_id,
                window_s=PING_HISTORY_WINDOW_S,
            )
        except Exception:
            logger.exception("Could not read the reporter's earlier pings")
            return None
        return build_ping_history(earlier, outcomes, anchor_message_id=anchor_message_id)

    # Images the analysis pass itself looks at. Cheap at low detail, but they
    # sit in front of the first post, so the count, bytes and wait are capped.
    _ANALYSIS_MAX_IMAGES = 3
    _ANALYSIS_MAX_TOTAL_BYTES = 1_500_000
    _ANALYSIS_IMAGE_BUDGET_S = 8.0

    async def _collect_analysis_images(
        self,
        payload: dict[str, Any],
        *,
        live: dict[int, Any],
        fetch_message: Any = None,
        fetch_unlisted: bool = False,
        ctx: str = "",
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """Up to a few images from the messages that matter most to this
        ping (ping_context.analysis_image_candidates), downscaled, for the
        analysis call. Fills in image_ids on those payload messages so the
        model can tie each image to its author.

        Before this the text pass was told nothing about images at all, and
        "unfortunately for this bozo" reached it without the role screenshot
        that was the whole point of it.

        Never raises and never holds the brief up past the time budget: on
        any failure the analysis just runs on text, as before.
        """
        messages = payload.get("messages") or []
        candidates = analysis_image_candidates(
            messages, payload.get("anchor_message_id"), payload.get("reporter")
        )
        if not candidates:
            return [], set()
        by_id = {m.get("id"): m for m in messages}
        images: list[dict[str, Any]] = []
        used: set[str] = set()
        total = 0

        async def _source(mid: int) -> Any:
            msg = live.get(mid)
            if msg is not None or fetch_message is None:
                return msg
            entry = by_id.get(mid)
            # Only spend a fetch on a stored message that showed media, unless
            # the payload predates attachments being recorded (replay).
            if (
                not fetch_unlisted
                and entry is not None
                and not (entry.get("attachments") or "http" in str(entry.get("content") or ""))
            ):
                return None
            try:
                return await fetch_message(mid)
            except Exception:
                return None

        async def _gather() -> None:
            nonlocal total
            max_dim = self.settings.openai_max_image_dim
            for mid in candidates:
                if len(images) >= self._ANALYSIS_MAX_IMAGES:
                    return
                msg = await _source(mid)
                if msg is None:
                    continue
                blobs: list[tuple[str, bytes]] = []
                for att in [a for c in media_carriers(msg) for a in c.attachments]:
                    if not self._is_image_attachment(att):
                        continue
                    limit = 5_000_000 if self._is_gif_attachment(att) else 10_000_000
                    if att.size and att.size > limit:
                        continue
                    try:
                        blobs.append((f"att:{att.id}", await att.read()))
                    except Exception:
                        continue
                    if len(images) + len(blobs) >= self._ANALYSIS_MAX_IMAGES:
                        break
                for item in self._extract_message_media_urls(msg):
                    if len(images) + len(blobs) >= self._ANALYSIS_MAX_IMAGES:
                        break
                    url = item.get("url")
                    if not isinstance(url, str) or not url:
                        continue
                    data = await self._fetch_url_bytes(url, max_bytes=5_000_000)
                    if data:
                        blobs.append((f"url:{url}", data))
                for key, data in blobs:
                    if key in used or len(images) >= self._ANALYSIS_MAX_IMAGES:
                        continue
                    try:
                        resized, content_type, _, _ = resize_image_bytes(data, max_dim)
                    except Exception:
                        continue
                    if total + len(resized) > self._ANALYSIS_MAX_TOTAL_BYTES:
                        continue
                    total += len(resized)
                    used.add(key)
                    image_id = f"img_{mid}_{len(images) + 1}"
                    images.append(
                        {
                            "id": image_id,
                            "data_url": to_data_url(resized, content_type),
                            "detail": self.settings.openai_image_detail,
                        }
                    )
                    entry = by_id.get(mid)
                    if entry is not None:
                        entry["image_ids"] = list(entry.get("image_ids") or []) + [image_id]

        t0 = time.monotonic()
        try:
            await asyncio.wait_for(_gather(), timeout=self._ANALYSIS_IMAGE_BUDGET_S)
        except asyncio.TimeoutError:
            logger.info(
                "Analysis images hit the %.0fs budget; using %d",
                self._ANALYSIS_IMAGE_BUDGET_S,
                len(images),
            )
        except Exception:
            logger.exception("Collecting analysis images failed; analysing text only")
        # A message whose image didn't make it must not claim one.
        kept = {img["id"] for img in images}
        for entry in messages:
            if entry.get("image_ids"):
                entry["image_ids"] = [i for i in entry["image_ids"] if i in kept]
        self._dlog_ctx(
            ctx,
            "Analysis images=%d bytes=%d in %.2fs",
            len(images),
            total,
            time.monotonic() - t0,
        )
        return images, used

    @staticmethod
    def _incident_signature(result: IncidentResult) -> tuple[object, ...]:
        return (
            result.summary,
            result.reporter_note,
            tuple((p.user_id, p.name, p.role, p.notes or "") for p in result.participants),
            tuple(result.signals),
            tuple((r.id, r.reason) for r in result.rule_refs),
            tuple(result.recommendations),
            result.draft_message,
            tuple((t.user_id, t.message_id) for t in result.reply_targets),
            tuple((d.user_id, d.text) for d in result.draft_replies),
            tuple((q.message_id, q.quote) for q in result.evidence_quotes),
            tuple(result.memory_suggestions.server_notes),
            tuple((n.user_id, n.label, n.evidence_message_id) for n in result.memory_suggestions.user_notes),
        )

    @staticmethod
    def _apply_handled_look(embed: discord.Embed, current_message: discord.Message) -> None:
        """Match what pressing Mark Handled does to an embed: green, no Draft
        reply, same "Marked Handled by X" attribution. Used when a background
        update (images, bare-ping follow-up) lands on a brief that's already
        been marked handled - the content should still catch up, but it must
        keep reading as resolved rather than quietly reopening it.
        """
        embed.color = discord.Color.green()
        for index, existing in enumerate(embed.fields):
            if existing.name == "Draft reply":
                embed.remove_field(index)
                break
        old_footer = ""
        if current_message.embeds and current_message.embeds[0].footer:
            old_footer = current_message.embeds[0].footer.text or ""
        marker = "Marked Handled by "
        idx = old_footer.find(marker)
        handled_note = old_footer[idx:] if idx != -1 else old_footer
        if handled_note:
            new_footer = embed.footer.text or ""
            embed.set_footer(text=f"{new_footer} | {handled_note}" if new_footer else handled_note)

    async def _maybe_update_brief_with_images(
        self,
        *,
        message: Any,
        view: IncidentView,
        base_result: IncidentResult,
        base_raw_result: dict[str, Any],
        messages: list[discord.Message],
        scan_label: str,
        title: str,
        context: tuple[str, str | None] | None,
        action_participants: list[dict[str, Any]],
        mod_role_id: int | None,
        persist_view: bool,
        ctx: str,
    ) -> None:
        target_user_ids: set[int] = {t.user_id for t in base_result.reply_targets}

        messages_by_id: dict[int, discord.Message] = {m.id: m for m in messages}
        source_channel = messages[-1].channel if messages else None

        evidence_message_ids: list[int] = []
        seen_ids: set[int] = set()
        for q in base_result.evidence_quotes:
            if q.message_id and q.message_id not in seen_ids:
                seen_ids.add(q.message_id)
                evidence_message_ids.append(q.message_id)
        for t in base_result.reply_targets:
            if t.message_id and t.message_id not in seen_ids:
                seen_ids.add(t.message_id)
                evidence_message_ids.append(t.message_id)

        reply_context_by_ref: dict[int, list[str]] = {}
        for mid in evidence_message_ids:
            msg = messages_by_id.get(mid)
            if msg is None:
                continue
            ref = msg.reference
            ref_id = getattr(ref, "message_id", None) if ref else None
            if not isinstance(ref_id, int):
                continue
            snippet = compress_text(msg.clean_content, max_len=160)
            if snippet:
                reply_context_by_ref.setdefault(ref_id, []).append(
                    f"{display_name(msg.author)}: {snippet}"
                )

        async def _fetch_message(mid: int) -> discord.Message | None:
            if mid in messages_by_id:
                return messages_by_id[mid]
            if not isinstance(source_channel, (discord.TextChannel, discord.Thread)):
                return None
            try:
                fetched = await source_channel.fetch_message(mid)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
            return fetched

        candidate_messages: list[tuple[int, discord.Message, list[str]]] = []
        candidate_ids: set[int] = set()

        # 0) Images from messages being replied to by evidence messages (reply-chain context).
        for ref_id, reply_lines in reply_context_by_ref.items():
            if ref_id in candidate_ids:
                continue
            ref_msg = await _fetch_message(ref_id)
            if ref_msg is None:
                continue
            candidate_ids.add(ref_id)
            candidate_messages.append((0, ref_msg, reply_lines))

        # 1) Images directly in evidence messages.
        for mid in evidence_message_ids:
            if mid in candidate_ids:
                continue
            msg = messages_by_id.get(mid)
            if msg is None:
                continue
            candidate_ids.add(mid)
            candidate_messages.append((1, msg, []))

        # 2) Images posted by the targeted users (if any).
        if target_user_ids:
            for msg in messages:
                if msg.id in candidate_ids:
                    continue
                if msg.author.id not in target_user_ids:
                    continue
                candidate_ids.add(msg.id)
                candidate_messages.append((2, msg, []))

        # 3) Messages carrying media with little or no text. An image can be the
        # whole violation, and nothing in its wording will say so, so it has to
        # be able to reach the vision pass without the text pass naming it first.
        for msg in messages:
            if msg.id in candidate_ids:
                continue
            if len(compress_text(msg.clean_content, max_len=300)) > 120:
                continue
            if not self._extract_message_media_urls(msg) and not any(
                self._is_image_attachment(a)
                for carrier in media_carriers(msg)
                for a in carrier.attachments
            ):
                continue
            candidate_ids.add(msg.id)
            candidate_messages.append((3, msg, []))

        # Collect up to a few media items (attachments + stickers + embed thumbnails).
        max_media = 3
        max_image_bytes = 10_000_000
        max_gif_bytes = 5_000_000
        max_url_bytes = 4_000_000
        max_gif_url_bytes = 5_000_000

        image_payloads: list[dict[str, Any]] = []
        image_meta: dict[str, dict[str, Any]] = {}
        # The analysis pass already saw these; paying to describe them again
        # would only restate what the brief already says.
        seen_media: set[str] = set(base_result.analysed_media)

        def _needs_high_detail(context_text: str, *, is_evidence_msg: bool) -> bool:
            if is_evidence_msg:
                return True
            t = (context_text or "").lower()
            keywords = (
                "scam",
                "scammer",
                "proof",
                "evidence",
                "screenshot",
                "dm",
                "direct message",
                "paypal",
                "venmo",
                "cashapp",
                "crypto",
                "bitcoin",
                "wallet",
                "gift card",
                "nitro",
                "steam",
                "http://",
                "https://",
            )
            return any(k in t for k in keywords)

        for _, msg, reply_lines in sorted(candidate_messages, key=lambda t: (t[0], t[1].created_at)):
            if len(image_payloads) >= max_media:
                break

            msg_text = compress_text(msg.clean_content, max_len=160)
            base_context_parts = [
                f"msg_id={msg.id}",
                f"author={display_name(msg.author)}",
            ]
            if msg_text:
                base_context_parts.append(f"text={msg_text}")
            if reply_lines:
                base_context_parts.append("replied_by=" + " | ".join(reply_lines[:2]))
            base_context = " | ".join(base_context_parts)

            is_evidence_msg = msg.id in evidence_message_ids
            needs_high = _needs_high_detail(base_context, is_evidence_msg=is_evidence_msg)
            detail = "high" if needs_high else self.settings.openai_image_detail
            max_dim = self.settings.openai_max_image_dim
            if needs_high:
                max_dim = max(max_dim, 1024)

            for attachment in [
                a for carrier in media_carriers(msg) for a in carrier.attachments
            ]:
                if len(image_payloads) >= max_media:
                    break
                if not self._is_image_attachment(attachment):
                    continue
                key = f"att:{attachment.id}"
                if key in seen_media:
                    continue
                seen_media.add(key)
                is_gif = self._is_gif_attachment(attachment)
                size_limit = max_gif_bytes if is_gif else max_image_bytes
                if attachment.size and attachment.size > size_limit:
                    continue
                try:
                    data = await attachment.read()
                except Exception:
                    continue
                resized, content_type, _, _ = resize_image_bytes(
                    data, max_dim
                )
                image_id = f"m{msg.id}_att{attachment.id}"
                image_payloads.append(
                    {
                        "id": image_id,
                        "data_url": to_data_url(resized, content_type),
                        "context": base_context,
                        "detail": detail,
                    }
                )
                image_meta[image_id] = {
                    "message_id": msg.id,
                    "author_name": display_name(msg.author),
                }

            for item in self._extract_message_media_urls(msg):
                if len(image_payloads) >= max_media:
                    break
                url = item.get("url")
                if not isinstance(url, str) or not url:
                    continue
                key = f"url:{url}"
                if key in seen_media:
                    continue
                seen_media.add(key)
                is_gif = bool(item.get("is_gif", False))
                limit = max_gif_url_bytes if is_gif else max_url_bytes
                data = await self._fetch_url_bytes(url, max_bytes=limit)
                if not data:
                    continue
                try:
                    resized, content_type, _, _ = resize_image_bytes(
                        data, max_dim
                    )
                except Exception:
                    continue

                kind = str(item.get("kind") or "url")
                sticker_id = item.get("sticker_id")
                suffix = ""
                if kind == "sticker" and sticker_id is not None:
                    suffix = f"stk{sticker_id}"
                else:
                    suffix = str(abs(hash(url)))[:8]
                image_id = f"m{msg.id}_{kind}{suffix}"
                image_payloads.append(
                    {
                        "id": image_id,
                        "data_url": to_data_url(resized, content_type),
                        "context": base_context,
                        "detail": detail,
                    }
                )
                image_meta[image_id] = {
                    "message_id": msg.id,
                    "author_name": display_name(msg.author),
                }

        self._dlog_ctx(ctx, "Background media candidates=%s", len(image_payloads))
        if not image_payloads:
            return

        openai_settings = OpenAISettings(
            api_key=self.settings.openai_api_key,
            model=self.settings.openai_model,
            image_detail=self.settings.openai_image_detail,
            debug_logs=self.settings.debug_logs,
        )

        # Summarize images and use notes to refine the brief.
        t0 = time.monotonic()
        summarized: list[dict[str, Any]] = []
        client = create_client(self.settings.openai_api_key)
        try:
            summarized = await asyncio.to_thread(
                summarize_images, client, openai_settings, image_payloads
            )
        except Exception:
            logger.exception("Background summarize_images failed")
            return
        finally:
            try:
                client.close()
            except Exception:
                pass
        self._dlog_ctx(
            ctx,
            "Background summarize_images returned=%s in %.2fs",
            len(summarized),
            time.monotonic() - t0,
        )

        image_notes: list[dict[str, Any]] = []
        for item in summarized:
            image_id = str(item.get("id") or "")
            note = str(item.get("note") or "").strip()
            if not image_id or not note:
                continue
            meta = image_meta.get(image_id)
            if not isinstance(meta, dict):
                continue
            message_id = meta.get("message_id")
            if not isinstance(message_id, int):
                continue
            author_name = str(meta.get("author_name") or "")
            image_notes.append(
                {
                    "image_id": image_id,
                    "message_id": message_id,
                    "author_name": author_name,
                    "note": note,
                    "is_evidence": bool(item.get("is_evidence", False)),
                    "is_context": bool(item.get("is_context", False)),
                }
            )
        if not image_notes:
            return

        self._dlog_ctx(ctx, "Background refine_incident_with_images images=%s", len(image_notes))
        t0 = time.monotonic()
        client = create_client(self.settings.openai_api_key)
        try:
            refined_raw = await asyncio.to_thread(
                refine_incident_with_images,
                client,
                openai_settings,
                base_raw_result,
                image_notes,
            )
        except Exception:
            logger.exception("Background refine_incident_with_images failed")
            return
        finally:
            try:
                client.close()
            except Exception:
                pass
        self._dlog_ctx(ctx, "Background refine_incident_with_images in %.2fs", time.monotonic() - t0)

        refined = parse_incident_result(refined_raw)
        # The refine prompt never sees the reporter or their ping history.
        refined.ping_history = base_result.ping_history
        refined.analysed_media = list(base_result.analysed_media)
        if not refined.reporter_note.strip():
            refined.reporter_note = base_result.reporter_note

        self._postprocess_result(refined, messages)

        message_links = {m.id: m.jump_url for m in messages}
        for q in refined.evidence_quotes:
            if q.link:
                continue
            if q.message_id and q.message_id in message_links:
                q.link = message_links[q.message_id]
        for note in refined.memory_suggestions.user_notes:
            if note.evidence_link:
                continue
            if note.evidence_message_id and note.evidence_message_id in message_links:
                note.evidence_link = message_links[note.evidence_message_id]

        if self._incident_signature(refined) == self._incident_signature(base_result):
            return

        now_handled = await self._brief_is_handled(message.id, view)
        new_embed = self._build_incident_embed(
            refined,
            title=title,
            scan_label=scan_label,
            context=context,
        )
        if now_handled:
            self._apply_handled_look(new_embed, message)
        new_payload = IncidentViewPayload(
            draft_message=refined.draft_message,
            reply_targets=[t.model_dump() for t in refined.reply_targets],
            draft_replies=[r.model_dump() for r in refined.draft_replies],
            memory_suggestions=refined.memory_suggestions.model_dump(),
            mod_role_id=mod_role_id,
            participants=action_participants,
            evidence_quotes=[q.model_dump() for q in refined.evidence_quotes],
            recommendations=list(refined.recommendations or []),
            rule_ids=[r.id for r in (refined.rule_refs or [])],
            source_channel_id=view.payload.source_channel_id,
            allow_post=view.payload.allow_post,
            allow_actions=view.payload.allow_actions,
            anchor_message_id=view.payload.anchor_message_id,
            handled=now_handled,
        )
        new_view = IncidentView(
            payload=new_payload,
            memory_store=self.memory_store,
            view_store=self.view_store,
        )

        try:
            await message.edit(embed=new_embed, view=new_view)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return

        if persist_view:
            record = ViewRecord(
                message_id=message.id,
                channel_id=message.channel.id,
                guild_id=message.guild.id if message.guild else 0,
                payload=new_payload.to_dict(),
                created_at=time.time(),
            )
            await self.view_store.save_view(record)

        self._dlog_ctx(ctx, "Updated brief after image check")

    async def _maybe_update_brief_with_followup(
        self,
        *,
        message: discord.Message,
        view: IncidentView,
        anchor: discord.Message,
        title: str,
        context: tuple[str, str | None] | None,
        mod_role_id: int | None,
        guild_id: int,
        ctx: str,
        schedule: tuple[int, ...] = _AUDIT_FOLLOW_UP_S,
    ) -> None:
        """Poll for what happens after a brief posts, and fold genuinely new
        evidence back into it: messages posted in the channel afterward, and
        what the audit log shows a moderator actually did - neither of which
        the initial scan can ever see, since it only looks backward from the
        ping and nothing has happened yet when it runs. A real incident
        showed both matter: a moderator's ban of someone else entirely
        vindicated a bare ping that the initial brief read as baseless, and
        the ban predated the follow-up-message check that existed before this
        one, so it was invisible to that too.

        Stops early once the brief is marked handled - a human closed this
        out, and Mark Handled now triggers its own evidence check (see
        IncidentView._attach_action_summary), so nothing is lost by not
        continuing to poll here after that.

        The initial brief posts immediately regardless of any of this - this
        only ever edits it afterward, never delays it.
        """
        channel = anchor.channel
        if not isinstance(channel, _AUTO_MOD_SOURCE_TYPES):
            return
        guild = anchor.guild
        if guild is None:
            return

        for wait_s in schedule:
            if wait_s:
                await asyncio.sleep(wait_s)
            if await self._brief_is_handled(message.id, view):
                self._dlog_ctx(ctx, "Stopping auto-brief follow-up polling - already handled")
                return
            await self._refresh_incident_evidence(
                message=message,
                view=view,
                anchor=anchor,
                channel=channel,
                guild=guild,
                title=title,
                context=context,
                mod_role_id=mod_role_id,
                guild_id=guild_id,
                ctx=ctx,
            )

    async def _brief_is_handled(self, message_id: int, view: IncidentView) -> bool:
        """Whether a moderator has closed this brief, from the stored record.

        Every rebuild attaches a new IncidentView to the message, so the
        object a background task was handed can be one a moderator never
        saw. On 2026-09-27 mod_b pressed Action taken on the second view;
        the follow-up poll, still holding the first, read handled=False at
        its 22-minute look and rebuilt the card with its buttons back. The
        button press saves the record, so that is the source of truth.
        """
        if view.payload.handled:
            return True
        store = getattr(self, "view_store", None)
        if store is None:
            return False
        try:
            record = await store.load_view(message_id)
        except Exception:
            logger.exception("Could not read stored view for %s", message_id)
            return False
        return bool(record and record.payload.get("handled"))

    async def _refresh_incident_evidence(
        self,
        *,
        message: discord.Message,
        view: IncidentView,
        anchor: discord.Message,
        channel: Any,
        guild: discord.Guild,
        title: str,
        context: tuple[str, str | None] | None,
        mod_role_id: int | None,
        guild_id: int,
        ctx: str,
        extra_target_user_ids: set[int] | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> tuple[discord.Embed, IncidentResult] | None:
        """One look for new evidence since the last analysis, and a
        recompute only if it found something. Safe to call repeatedly, and
        from more than one trigger (the auto-brief's own follow-up poll, and
        Mark Handled): everything it needs to avoid redoing work comes from
        the persisted incident payload, not from in-process state, so
        whichever trigger runs next picks up exactly where the last one
        left off, and a process restart loses nothing already found.

        Evidence is accumulated, never replaced: a later look that
        legitimately finds less (an audit-log page a busy guild pushed an
        old entry off, a follow-up message someone deleted) still keeps
        everything an earlier look already knew, via merge_new_lines and an
        id-keyed union of messages.

        dry_run=True runs the same look and, if there's anything new, the
        same analysis - but touches neither Discord nor storage, and hands
        the caller back the embed and result it would have applied. Used by
        `python -m incident_mod_bot.refresh_brief` to preview before editing
        a live brief.

        force=True recomputes and rebuilds the embed even when this look
        finds nothing new - for re-rendering a brief against evidence that
        was already known but wasn't yet reflected in the card (e.g. after
        a code change to how the Action-taken field or the prompt itself
        handles it). Never used by the bot's own automatic polling.
        """
        stored = await self.memory_store.get_incident_payload(message.id)
        if stored is None:
            self._dlog_ctx(ctx, "No stored incident payload - nothing to refresh from")
            return
        _, base_payload = stored
        known_messages: list[dict[str, Any]] = list(base_payload.get("messages") or [])
        known_ids = {m.get("id") for m in known_messages}
        known_audit: list[str] = list(base_payload.get("audit_findings") or [])

        try:
            follow_ups = [
                m
                async for m in channel.history(
                    after=anchor, limit=_BARE_PING_FOLLOWUP_SCAN_LIMIT, oldest_first=True
                )
                if not m.author.bot
            ]
        except (discord.Forbidden, discord.HTTPException):
            follow_ups = []
        new_follow_ups = [m for m in follow_ups if m.id not in known_ids]

        try:
            found_audit = await view._collect_recent_mod_actions(
                SimpleNamespace(guild=guild),
                since=anchor.created_at - timedelta(seconds=_AUDIT_LOOKBACK_BEFORE_S),
                extra_target_user_ids=extra_target_user_ids,
            )
        except Exception:
            logger.exception("Audit-log check failed during incident evidence refresh")
            found_audit = []

        merged_audit, audit_changed = merge_new_lines(known_audit, found_audit)
        if not new_follow_ups and not audit_changed and not force:
            return

        new_compressed = self._compress_messages(new_follow_ups, set())
        merged_messages = known_messages + [m for m in new_compressed if m["id"] not in known_ids]

        # Persist the growth before spending an OpenAI call on it - a later
        # look, from any trigger, must never start from less than this one found.
        if not dry_run:
            try:
                await self.memory_store.save_incident_payload(
                    message.id,
                    guild_id,
                    {**base_payload, "messages": merged_messages, "audit_findings": merged_audit},
                )
            except Exception:
                logger.exception("Failed to persist accumulated incident evidence")

        self._dlog_ctx(
            ctx,
            "New incident evidence: %d new message(s), audit_changed=%s",
            len(new_follow_ups),
            audit_changed,
        )

        try:
            result, raw_result, analysis_payload = await self._analyze_incident_messages(
                guild_id=guild_id,
                messages=new_follow_ups,
                mod_role_id=mod_role_id,
                anchor_message_id=anchor.id,
                ctx=ctx,
                precompressed_base=[
                    m for m in known_messages if m["id"] not in {mm.id for mm in new_follow_ups}
                ],
                audit_findings=merged_audit,
                # Briefs saved before the reporter was recorded (and the
                # manual refresh tool, run against one) still have a ping
                # as their anchor - recognise it from the message itself.
                reporter=base_payload.get("reporter")
                or (_ping_reporter(anchor) if anchor.raw_role_mentions else None),
                prior_authors=base_payload.get("authors"),
                extra_messages=[anchor, *_resolved_reply(anchor)],
                fetch_message=getattr(channel, "fetch_message", None),
            )
        except AuthenticationError:
            logger.exception("OpenAI auth error during incident evidence refresh")
            return
        except Exception:
            logger.exception("Incident evidence refresh analysis failed")
            return

        action_participants: list[dict[str, Any]] = []
        seen_users: set[int] = set()
        for msg in new_follow_ups:
            user_id = msg.author.id
            if user_id in seen_users:
                continue
            seen_users.add(user_id)
            m = msg.author if isinstance(msg.author, discord.Member) else None
            action_participants.append(
                {
                    "user_id": user_id,
                    "name": display_name(msg.author),
                    "role": "mod" if is_mod(m, mod_role_id) else "member",
                }
            )
            if len(action_participants) >= 25:
                break
        if not action_participants:
            # An audit-only trigger (no new channel messages) still needs
            # somewhere to draw participants from.
            action_participants = list(view.payload.participants or [])

        # Checked again here, as late as possible: analysis just took a real
        # amount of time, and a moderator may have pressed Mark Handled while
        # it ran. Either way the content below is worth showing - it just
        # must not reopen something that's already resolved.
        now_handled = await self._brief_is_handled(message.id, view)
        if now_handled:
            self._dlog_ctx(ctx, "Incident evidence ready on a handled brief - updating content only")

        scan_label = f"{len(merged_messages)} msgs"
        new_embed = self._build_incident_embed(
            result,
            title=title,
            scan_label=scan_label,
            context=context,
        )
        if merged_audit:
            # The rebuild starts from a fresh embed, which would otherwise
            # drop this field entirely - and the prompt is told not to
            # restate the same outcome in the summary, so it isn't
            # duplicated between prose and this list either.
            body = "\n".join(f"• {line}" for line in merged_audit)[:1024]
            new_embed.add_field(name=_ACTION_FIELD, value=body, inline=False)
        if now_handled:
            self._apply_handled_look(new_embed, message)
        if dry_run:
            return new_embed, result
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
            source_channel_id=view.payload.source_channel_id,
            allow_post=view.payload.allow_post,
            allow_actions=view.payload.allow_actions,
            anchor_message_id=anchor.id,
            handled=now_handled,
        )
        new_view = IncidentView(
            payload=new_payload,
            memory_store=self.memory_store,
            view_store=self.view_store,
        )
        try:
            await message.edit(embed=new_embed, view=new_view)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return

        record = ViewRecord(
            message_id=message.id,
            channel_id=message.channel.id,
            guild_id=message.guild.id if message.guild else 0,
            payload=new_payload.to_dict(),
            created_at=time.time(),
        )
        await self.view_store.save_view(record)
        try:
            await self.memory_store.save_incident_payload(
                message.id,
                guild_id,
                {
                    **analysis_payload,
                    "audit_findings": merged_audit,
                    "source_channel_id": view.payload.source_channel_id,
                },
            )
        except Exception:
            logger.exception("Failed to persist refreshed incident payload")

        self._dlog_ctx(
            ctx,
            "Updated brief with new evidence: %s",
            (getattr(result, "headline", "") or "")[:80],
        )
        return new_embed, result

    async def _fetch_recent_messages_ending_at(
        self,
        channel: discord.abc.Messageable,
        *,
        limit: int,
        end_message: discord.Message,
    ) -> list[discord.Message]:
        messages: list[discord.Message] = []
        before_limit = max(limit - 1, 0)
        async for message in channel.history(limit=before_limit, before=end_message):
            if message.author.bot:
                continue
            messages.append(message)
        out = list(reversed(messages))
        if not end_message.author.bot:
            out.append(end_message)
        return out

    async def _fetch_recent_messages(
        self, channel: discord.abc.Messageable, limit: int
    ) -> list[discord.Message]:
        messages: list[discord.Message] = []
        async for message in channel.history(limit=limit):
            if message.author.bot:
                continue
            messages.append(message)
        return list(reversed(messages))

    async def _fetch_all_text(self, channel: discord.TextChannel) -> tuple[str, int, int]:
        scanned = 0
        kept = 0
        parts: list[str] = []
        async for message in channel.history(limit=None, oldest_first=True):
            scanned += 1
            if message.author.bot:
                continue
            content = message.clean_content.strip()
            if content:
                parts.append(content)
                kept += 1
        return "\n".join(parts), scanned, kept

    async def _prepare_images(
        self, messages: list[discord.Message], *, max_images: int | None = None
    ) -> tuple[list[dict[str, str]], dict[str, str], int]:
        image_payloads: list[dict[str, str]] = []
        image_links: dict[str, str] = {}
        image_count = 0
        total_images = 0
        limit = max_images if max_images is not None else self.settings.max_images_to_analyze
        max_image_bytes = 10_000_000
        max_gif_bytes = 5_000_000
        for message in messages:
          for carrier in media_carriers(message):
            for attachment in carrier.attachments:
                if not self._is_image_attachment(attachment):
                    continue
                total_images += 1
                if image_count >= limit:
                    continue
                is_gif = (
                    (attachment.content_type or "").lower().startswith("image/gif")
                    or attachment.filename.lower().endswith(".gif")
                )
                size_limit = max_gif_bytes if is_gif else max_image_bytes
                if attachment.size and attachment.size > size_limit:
                    continue
                try:
                    data = await attachment.read()
                except Exception:
                    continue
                resized, content_type, _, _ = resize_image_bytes(data, self.settings.openai_max_image_dim)
                image_id = f"img_{message.id}_{attachment.id}"
                image_payloads.append(
                    {
                        "id": image_id,
                        "data_url": to_data_url(resized, content_type),
                    }
                )
                image_links[image_id] = message.jump_url
                image_count += 1
        omitted = max(total_images - image_count, 0)
        return image_payloads, image_links, omitted

    def _compress_messages(
        self,
        messages: list[discord.Message],
        included_image_ids: set[str],
    ) -> list[dict[str, Any]]:
        compressed: list[dict[str, Any]] = []
        for message in messages:
            content = compress_text(message.clean_content, max_len=300)
            forwarded = forwarded_content(message)
            if forwarded:
                content = compress_text(
                    f"{content} [forwarded] {forwarded}".strip(), max_len=300
                )
            image_ids: list[str] = []
            # Every attachment is named, with its size if it's an image, so the
            # model knows a message carried a picture even when the picture
            # itself isn't sent. An image-only message used to reach it as "".
            attachments: list[str] = []
            for carrier in media_carriers(message):
                for attachment in carrier.attachments:
                    attachments.append(self._describe_attachment(attachment))
                    if not self._is_image_attachment(attachment):
                        continue
                    image_id = f"img_{message.id}_{attachment.id}"
                    if image_id in included_image_ids:
                        image_ids.append(image_id)
            entry: dict[str, Any] = {
                "id": message.id,
                "author_id": message.author.id,
                "author_name": display_name(message.author),
                "content": content,
                "image_ids": image_ids,
            }
            if attachments:
                entry["attachments"] = attachments[:4]
            # Who is answering whom. A forward also carries a reference, but
            # its text is already folded into content above.
            ref_id = getattr(message.reference, "message_id", None) if message.reference else None
            if message.type == discord.MessageType.reply and isinstance(ref_id, int):
                entry["reply_to"] = ref_id
            compressed.append(entry)
        return compressed

    async def _collect_user_memory(
        self, guild_id: int, messages: list[discord.Message]
    ) -> list[dict[str, Any]]:
        seen: set[int] = set()
        memory: list[dict[str, Any]] = []
        for message in messages:
            user_id = message.author.id
            if user_id in seen:
                continue
            seen.add(user_id)
            entries = await self.memory_store.list_user_profile_entries(guild_id, user_id)
            if not entries:
                continue
            summary = "\n".join(f"- {label} (seen {count}x)" for label, count in entries)
            memory.append({"user_id": user_id, "summary": summary})
        return memory

    @staticmethod
    def _describe_attachment(attachment: discord.Attachment) -> str:
        name = compress_text(attachment.filename or "file", max_len=40)
        width, height = getattr(attachment, "width", None), getattr(attachment, "height", None)
        if width and height:
            return f"{name} ({width}x{height} image)"
        kind = (attachment.content_type or "").split(";")[0]
        return f"{name} ({kind})" if kind else name

    @staticmethod
    def _is_image_attachment(attachment: discord.Attachment) -> bool:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            return True
        name = attachment.filename.lower()
        return name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"))

    @staticmethod
    def _is_gif_attachment(attachment: discord.Attachment) -> bool:
        if (attachment.content_type or "").lower().startswith("image/gif"):
            return True
        return attachment.filename.lower().endswith(".gif")

    @staticmethod
    def _is_probably_gif_url(url: str) -> bool:
        u = url.lower()
        return ".gif" in u or "tenor" in u or "giphy" in u

    @staticmethod
    def _extract_embed_media_urls(embed: discord.Embed) -> list[str]:
        urls: list[str] = []
        try:
            image_url = getattr(embed.image, "url", None)
            if isinstance(image_url, str) and image_url.strip():
                urls.append(image_url.strip())
        except Exception:
            pass
        try:
            thumb_url = getattr(embed.thumbnail, "url", None)
            if isinstance(thumb_url, str) and thumb_url.strip():
                urls.append(thumb_url.strip())
        except Exception:
            pass
        # De-dupe while preserving order.
        out: list[str] = []
        seen: set[str] = set()
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
        return out

    def _extract_message_media_urls(self, message: discord.Message) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for carrier in media_carriers(message):
            out.extend(self._media_urls_of(carrier, seen))
        return out

    def _media_urls_of(self, message: Any, seen: set[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        for sticker in getattr(message, "stickers", []) or []:
            try:
                url = sticker.url
                fmt = sticker.format
            except Exception:
                continue
            if not isinstance(url, str) or not url:
                continue
            if url in seen:
                continue
            # Skip lottie stickers.
            if fmt == discord.StickerFormatType.lottie:
                continue
            seen.add(url)
            out.append(
                {
                    "kind": "sticker",
                    "url": url,
                    "sticker_id": getattr(sticker, "id", None),
                    "is_gif": fmt == discord.StickerFormatType.gif,
                }
            )

        for embed in message.embeds or []:
            for url in self._extract_embed_media_urls(embed):
                if url in seen:
                    continue
                seen.add(url)
                out.append({"kind": "embed", "url": url, "is_gif": self._is_probably_gif_url(url)})

        return out

    async def _fetch_url_bytes(self, url: str, *, max_bytes: int, timeout_s: float = 10.0) -> bytes | None:
        if not url:
            return None
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    length = resp.headers.get("Content-Length")
                    if length:
                        try:
                            if int(length) > max_bytes:
                                return None
                        except ValueError:
                            pass
                    data = bytearray()
                    async for chunk in resp.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            return None
                    return bytes(data)
        except Exception:
            return None

    # Brief layout is deliberately inverted: verdict and draft reply first,
    # audit trail last. Moderators read the synopsis and press one button, so
    # anything they cannot act on is demoted or dropped.
    LOW_CONFIDENCE = 0.6

    @staticmethod
    def _is_actor(p) -> bool:
        """A participant who did something, as opposed to being in the channel."""
        role = (p.role or "").strip().lower()
        return bool(p.notes) or role not in {"", "member", "user", "bystander"}

    def _build_incident_embed(
        self,
        result: IncidentResult,
        *,
        title: str = "Mod Brief",
        scan_label: str | None = None,
        context: tuple[str, str | None] | None = None,
    ) -> discord.Embed:
        headline = (getattr(result, "headline", "") or "").strip()
        # The title is the slot people skim past, so it carries what raised the
        # brief rather than its content. Giving the embed a url makes the whole
        # title a blue link, which is the only way to get a visible link up
        # there: titles render no markdown and no mentions.
        context_text, context_url = context if context else (None, None)
        embed = discord.Embed(
            title=truncate(context_text or headline or title, 240),
            url=context_url or None,
            color=discord.Color.orange(),
        )

        # What happened comes first and the action second, with a blank line
        # between them. Reading them as one paragraph made the description look
        # like a continuation of the instruction.
        lines: list[str] = []
        # A fixed label, so every card has the same thing in the same place and
        # it pairs with **Do:** below. "What happened" is what moderators write
        # in chat-discussion; "incident" and "flashpoint" are not.
        lines.append(f"**What:** {truncate(result.summary, 400)}")
        # The pinger's own conduct and their earlier pings, when there is any.
        # Facts for a moderator to weigh, never a verdict on them.
        pinger = " ".join(
            part.strip().rstrip(".") + "."
            for part in (result.reporter_note or "", result.ping_history or "")
            if part.strip()
        )
        if pinger:
            lines.append(f"**Pinger:** {truncate(pinger, 400)}")
        do_lines: list[str] = []
        if result.recommendations:
            # Almost always one combined action ("Remove the post and ban");
            # a second item is only for a genuinely separate action against a
            # different person. Rule ids are internal - they inform the model,
            # they don't belong on the card.
            do = " · ".join(r.rstrip(".") for r in result.recommendations[:2])
            do_lines.append(f"**Do:** {truncate(do, 300)}")
        body = "\n".join(line for line in lines if line)
        if do_lines:
            body += "\n\n" + "\n".join(do_lines)
        embed.description = body

        # Who's involved and what they did is already in the summary above;
        # a separate field just repeated it word for word on the common case
        # of a single actor, so it's gone. _is_actor still gates whether the
        # incident is complex enough for "What happened" below.
        actors = [p for p in result.participants if self._is_actor(p)]

        # Key Moments restates summary+evidence for simple incidents. Keep it
        # only where the narrative earns its place: 3+ people actually involved.
        if result.signals and len(actors) >= 3:
            embed.add_field(
                name="What happened",
                value=truncate("\n".join(f"- {s}" for s in result.signals[:4]), 1024),
                inline=False,
            )

        draft_lines: list[str] = []
        if result.draft_replies:
            for item in result.draft_replies[:3]:
                text = item.text.strip()
                if text:
                    draft_lines.append(f"<@{item.user_id}> {text}".strip())
        else:
            prefix = " ".join(f"<@{t.user_id}>" for t in result.reply_targets[:3]).strip()
            text = result.draft_message.strip()
            if prefix and text:
                draft_lines.append(f"{prefix} {text}".strip())
            elif text:
                draft_lines.append(text)
        if draft_lines:
            embed.add_field(name="Draft reply", value=truncate("\n".join(draft_lines), 1024), inline=False)

        if result.evidence_quotes:
            quotes = [f"\"{q.quote}\" [jump]({q.link})" for q in result.evidence_quotes[:2] if q.link]
            if quotes:
                embed.add_field(name="Evidence", value=truncate("\n".join(quotes), 1024), inline=False)

        # Confidence is only actionable as a warning; a number nobody acts on is noise.
        if result.confidence and result.confidence < self.LOW_CONFIDENCE:
            embed.set_footer(text="Low confidence - please verify before acting")
        return embed

    async def _restore_views(self) -> None:
        await self.view_store.prune(ttl_s=48 * 3600)
        records = await self.view_store.load_views()
        restored = 0
        for record in records:
            channel = self.get_channel(record.channel_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await self.view_store.delete_view(record.message_id)
                continue
            try:
                fetched_message = await channel.fetch_message(record.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await self.view_store.delete_view(record.message_id)
                continue
            payload = record.payload
            needs_migration = payload.get("view_version") != 2
            memory_suggestions = payload.get("memory_suggestions")
            if not isinstance(memory_suggestions, dict):
                memory_suggestions = {}
            mod_role_id = payload.get("mod_role_id")
            if mod_role_id is not None and not isinstance(mod_role_id, int):
                mod_role_id = None
            participants = payload.get("participants")
            if not isinstance(participants, list):
                participants = []
            evidence_quotes = payload.get("evidence_quotes")
            if not isinstance(evidence_quotes, list):
                evidence_quotes = []
            source_channel_id = payload.get("source_channel_id")
            if source_channel_id is not None and not isinstance(source_channel_id, int):
                source_channel_id = None
            allow_post = payload.get("allow_post")
            allow_post_bool = bool(allow_post) if isinstance(allow_post, bool) else False
            allow_actions = payload.get("allow_actions")
            allow_actions_bool = bool(allow_actions) if isinstance(allow_actions, bool) else False
            handled = payload.get("handled")
            handled_bool = bool(handled) if isinstance(handled, bool) else False
            reply_targets = payload.get("reply_targets")
            if not isinstance(reply_targets, list):
                reply_targets = []
            draft_replies = payload.get("draft_replies")
            if not isinstance(draft_replies, list):
                draft_replies = []
            anchor_message_id = payload.get("anchor_message_id")
            if anchor_message_id is not None and not isinstance(anchor_message_id, int):
                anchor_message_id = None
            view_payload = IncidentViewPayload(
                view_version=3,
                recommendations=list(payload.get("recommendations") or []),
                rule_ids=list(payload.get("rule_ids") or []),
                draft_message=str(payload.get("draft_message", "")),
                reply_targets=reply_targets,
                draft_replies=draft_replies,
                memory_suggestions=memory_suggestions,
                mod_role_id=mod_role_id,
                participants=participants,
                evidence_quotes=evidence_quotes,
                source_channel_id=source_channel_id,
                allow_post=allow_post_bool,
                allow_actions=allow_actions_bool,
                anchor_message_id=anchor_message_id,
                handled=handled_bool,
            )
            view = IncidentView(
                payload=view_payload,
                memory_store=self.memory_store,
                view_store=self.view_store,
            )
            try:
                self.add_view(view, message_id=record.message_id)
            except ValueError:
                # Non-persistent views (missing custom_id / timeout) cannot be restored.
                await self.view_store.delete_view(record.message_id)
                continue
            restored += 1

            if needs_migration:
                try:
                    await fetched_message.edit(view=view)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass
                try:
                    migrated = ViewRecord(
                        message_id=record.message_id,
                        channel_id=record.channel_id,
                        guild_id=record.guild_id,
                        payload=view_payload.to_dict(),
                        created_at=record.created_at,
                    )
                    await self.view_store.save_view(migrated)
                except Exception:
                    pass
        if restored:
            logger.info("Restored %s incident views", restored)


def main() -> None:
    load_dotenv()
    settings = load_settings()
    bot = IncidentBot(settings)
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()
