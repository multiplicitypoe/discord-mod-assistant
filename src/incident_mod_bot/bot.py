from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import discord
import httpx
from discord import app_commands
from dotenv import load_dotenv
from openai import AuthenticationError

from incident_mod_bot.config import Settings, load_settings
from incident_mod_bot.discord_ui.incident_view import IncidentView, IncidentViewPayload
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
from incident_mod_bot.pipeline.incident import IncidentResult, ReplyTarget, parse_incident_result
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

# A ping with no text of its own is a strong tell that the reporter is about
# to explain in their very next message - waiting this long catches it before
# deciding the brief is final. Restricted to the reporter's own follow-ups,
# capped low: this is meant to catch "wait, let me explain", not fold in
# whatever else the channel says in the meantime.
_BARE_PING_FOLLOWUP_WAIT_S = 60
_BARE_PING_FOLLOWUP_MAX_MESSAGES = 3
_BARE_PING_FOLLOWUP_SCAN_LIMIT = 30


def _is_bare_ping(message: discord.Message) -> bool:
    """Whether a ping message carries no explanation of its own."""
    return not _MENTION_RE.sub("", message.content).strip()


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

    def _postprocess_result(self, result: IncidentResult, messages: list[discord.Message]) -> None:
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
            )
        except AuthenticationError:
            logger.exception("OpenAI auth error during auto /mod")
            return
        except Exception:
            logger.exception("Auto /mod failed")
            return

        ping_author = display_name(message.author)
        # The verb carries the link. A trailing "(jump)" is a second thing to read
        # for the same destination.
        context = (
            f"{ping_author} pinged {role_names} in #{source_parent.name}",
            message.jump_url,
        )
        embed = self._build_incident_embed(
            result,
            title="Auto Mod Brief",
            scan_label=scan_label,
            context=context,
            informed_by=result.informed_by,
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

        if _is_bare_ping(message):
            asyncio.create_task(
                self._maybe_update_brief_with_followup(
                    message=posted,
                    view=view,
                    anchor=message,
                    base_result=result,
                    messages=messages,
                    scan_label=scan_label,
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
        embed = self._build_incident_embed(result, scan_label=scan_label, informed_by=result.informed_by)

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

        embed = self._build_incident_embed(
            result, scan_label=scan_label, context=context, informed_by=result.informed_by
        )

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
    ) -> tuple[IncidentResult, dict[str, Any], dict[str, Any]]:
        # Text-first analysis. Images are handled in a background refinement step.
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

        image_ids: set[str] = set()

        self._dlog_ctx(
            ctx,
            "Context rules=%s server_memory=%s user_memory=%s images=%s",
            "yes" if rules_memory_raw else "no",
            len(server_memory),
            len(user_memory),
            len(image_ids),
        )

        payload = {
            "rules_memory": rules_memory,
            "server_memory": server_memory,
            "user_memory": user_memory,
            "messages": self._compress_messages(messages, image_ids),
        }
        if anchor_message_id is not None:
            payload["anchor_message_id"] = anchor_message_id

        self._dlog_ctx(ctx, "OpenAI analyze_incident model=%s", openai_settings.model)
        t0 = time.monotonic()

        async def _run_analysis() -> dict[str, Any]:
            client = create_client(self.settings.openai_api_key)
            try:
                return await asyncio.to_thread(analyze_incident, client, openai_settings, payload)
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        raw_result = await asyncio.create_task(_run_analysis())
        result = parse_incident_result(raw_result)
        # Carry through how much past enforcement shaped this brief, so the
        # ledger's contribution is visible instead of silent.
        result.informed_by = len(enforcement_norms)

        self._postprocess_result(result, messages)
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

    @staticmethod
    def _incident_signature(result: IncidentResult) -> tuple[object, ...]:
        return (
            result.summary,
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
        seen_media: set[str] = set()

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

        new_embed = self._build_incident_embed(
            refined,
            title=title,
            scan_label=scan_label,
            context=context,
            informed_by=refined.informed_by,
        )
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
            handled=False,
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
        base_result: IncidentResult,
        messages: list[discord.Message],
        scan_label: str,
        title: str,
        context: tuple[str, str | None] | None,
        mod_role_id: int | None,
        guild_id: int,
        ctx: str,
    ) -> None:
        """A bare ping almost always gets explained a moment later, in a
        message the initial scan window couldn't have seen - it only looks
        backward from the ping. Wait, then fold the reporter's own follow-up
        into a fresh analysis, rather than leave a brief built on an
        unexplained ping and whatever ambiguous prior chat happened to be
        nearby. The initial brief posts immediately regardless - this only
        ever edits it afterward, never delays it.
        """
        await asyncio.sleep(_BARE_PING_FOLLOWUP_WAIT_S)

        channel = anchor.channel
        if not isinstance(channel, _AUTO_MOD_SOURCE_TYPES):
            return
        try:
            after_messages = [
                m
                async for m in channel.history(
                    after=anchor, limit=_BARE_PING_FOLLOWUP_SCAN_LIMIT, oldest_first=True
                )
            ]
        except (discord.Forbidden, discord.HTTPException):
            return
        follow_ups = [
            m for m in after_messages if m.author.id == anchor.author.id and not m.author.bot
        ][:_BARE_PING_FOLLOWUP_MAX_MESSAGES]
        if not follow_ups:
            self._dlog_ctx(ctx, "Bare ping had no follow-up from the reporter")
            return

        # A moderator may already have acted in the minute this took to wait
        # for - don't reopen something that's already resolved.
        if view.payload.handled:
            self._dlog_ctx(ctx, "Bare ping follow-up found, but the brief is already handled")
            return

        # messages was fetched once at the original ping and is held in
        # memory, not re-fetched here - so if the reported content gets
        # deleted in the minute this waited (routine: deletion is often the
        # enforcement action itself), the re-analysis still sees exactly
        # what the original brief saw. Only follow_ups is freshly fetched.
        augmented = messages + follow_ups
        try:
            result, raw_result, analysis_payload = await self._analyze_incident_messages(
                guild_id=guild_id,
                messages=augmented,
                mod_role_id=mod_role_id,
                anchor_message_id=anchor.id,
                ctx=ctx,
            )
        except AuthenticationError:
            logger.exception("OpenAI auth error during bare-ping follow-up analysis")
            return
        except Exception:
            logger.exception("Bare-ping follow-up analysis failed")
            return

        # The reporter's follow-up doesn't always change the read - "he did
        # it again" adds nothing a moderator couldn't already see. Compare
        # against what was already posted and only touch the card when the
        # follow-up actually moved something.
        if self._incident_signature(result) == self._incident_signature(base_result):
            self._dlog_ctx(ctx, "Bare ping follow-up did not change the read")
            return

        action_participants: list[dict[str, Any]] = []
        seen_users: set[int] = set()
        for msg in augmented:
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

        if view.payload.handled:
            # Checked again: analysis just took a real amount of time.
            self._dlog_ctx(ctx, "Bare ping follow-up ready, but the brief was handled meanwhile")
            return

        new_embed = self._build_incident_embed(
            result,
            title=title,
            scan_label=scan_label,
            context=context,
            informed_by=result.informed_by,
        )
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
            handled=False,
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
                {**analysis_payload, "source_channel_id": view.payload.source_channel_id},
            )
        except Exception:
            logger.exception("Failed to persist incident payload for replay (follow-up)")

        self._dlog_ctx(
            ctx,
            "Updated a bare-ping brief after the reporter's follow-up: %s",
            (getattr(result, "headline", "") or "")[:80],
        )

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
            for carrier in media_carriers(message):
                for attachment in carrier.attachments:
                    if not self._is_image_attachment(attachment):
                        continue
                    image_id = f"img_{message.id}_{attachment.id}"
                    if image_id in included_image_ids:
                        image_ids.append(image_id)
            compressed.append(
                {
                    "id": message.id,
                    "author_id": message.author.id,
                    "author_name": display_name(message.author),
                    "content": content,
                    "image_ids": image_ids,
                }
            )
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
        informed_by: int = 0,
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
        if informed_by:
            observation = "observation" if informed_by == 1 else "observations"
            body += f"\n\n*Informed by {informed_by} enforcement {observation}.*"
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
