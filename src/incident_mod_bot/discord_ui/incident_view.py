from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import discord

from incident_mod_bot.discord_ui.view_store import ViewRecord, ViewStore
from incident_mod_bot.memory.store import MemoryStore
from incident_mod_bot.utils.discord import is_mod

logger = logging.getLogger("incident_mod_bot")


@dataclass(frozen=True)
class IncidentViewPayload:
    draft_message: str
    memory_suggestions: dict[str, Any]
    mod_role_id: int | None
    participants: list[dict[str, Any]]
    evidence_quotes: list[dict[str, Any]]
    reply_targets: list[dict[str, Any]] = field(default_factory=list)
    draft_replies: list[dict[str, Any]] = field(default_factory=list)
    source_channel_id: int | None = None
    allow_post: bool = True
    allow_actions: bool = True
    handled: bool = False
    # Bump when component custom_id layout changes.
    view_version: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "view_version": self.view_version,
            "draft_message": self.draft_message,
            "reply_targets": self.reply_targets,
            "draft_replies": self.draft_replies,
            "memory_suggestions": self.memory_suggestions,
            "mod_role_id": self.mod_role_id,
            "participants": self.participants,
            "evidence_quotes": self.evidence_quotes,
            "source_channel_id": self.source_channel_id,
            "allow_post": self.allow_post,
            "allow_actions": self.allow_actions,
            "handled": self.handled,
        }


def _truncate_one_line(text: str, max_len: int) -> str:
    s = " ".join(text.replace("\n", " ").split())
    if len(s) <= max_len:
        return s
    return s[: max(0, max_len - 3)] + "..."
class _ParticipantSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        options: list[discord.SelectOption],
        parent: "IncidentView",
        row: int,
    ) -> None:
        super().__init__(
            placeholder="Select user(s) for actions...",
            min_values=0,
            max_values=min(len(options), 10),
            options=options,
            row=row,
            custom_id="incident_select_participants",
        )
        # Avoid discord.py internal Item._parent attribute.
        self._incident_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        selected: list[int] = []
        for value in self.values:
            try:
                selected.append(int(value))
            except ValueError:
                continue
        self._incident_view.selected_user_ids = selected
        await interaction.response.defer()


class _EvidenceSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        options: list[discord.SelectOption],
        parent: "IncidentView",
        row: int,
    ) -> None:
        super().__init__(
            placeholder="Select evidence message(s) to delete...",
            min_values=0,
            max_values=min(len(options), 10),
            options=options,
            row=row,
            custom_id="incident_select_evidence",
        )
        # Avoid discord.py internal Item._parent attribute.
        self._incident_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        selected: list[int] = []
        for value in self.values:
            try:
                selected.append(int(value))
            except ValueError:
                continue
        self._incident_view.selected_message_ids = selected
        await interaction.response.defer()


class _ConfirmActionView(discord.ui.View):
    def __init__(self, *, on_confirm: Any) -> None:
        super().__init__(timeout=60)
        self._on_confirm = on_confirm

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            text = await self._on_confirm(interaction)
        except Exception as exc:
            logger.exception("UI confirm action failed")
            await interaction.edit_original_response(content=f"Action failed: {exc}", view=None)
            self.stop()
            return
        await interaction.edit_original_response(content=text, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


class _TimeoutModal(discord.ui.Modal):
    def __init__(self, *, user_ids: list[int], mod_role_id: int | None) -> None:
        super().__init__(title="Timeout Users")
        self._user_ids = user_ids
        self._mod_role_id = mod_role_id
        self.duration = discord.ui.TextInput(
            label="Duration",
            placeholder="e.g. 10m, 2h, 1d (max 28d)",
            default="10m",
            required=True,
            max_length=16,
        )
        self.reason = discord.ui.TextInput(
            label="Reason (optional)",
            placeholder="Shown in audit log",
            required=False,
            max_length=256,
        )
        self.add_item(self.duration)
        self.add_item(self.reason)

    @staticmethod
    def _parse_duration(text: str) -> timedelta | None:
        s = text.strip().lower()
        if not s:
            return None
        unit = "m"
        if s[-1] in {"m", "h", "d"}:
            unit = s[-1]
            s = s[:-1].strip()
        try:
            value = int(s)
        except ValueError:
            return None
        if value <= 0:
            return None
        if unit == "m":
            return timedelta(minutes=value)
        if unit == "h":
            return timedelta(hours=value)
        if unit == "d":
            return timedelta(days=value)
        return None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not is_mod(member, self._mod_role_id):
            await interaction.response.send_message("Mod permissions required.", ephemeral=True)
            return

        duration = self._parse_duration(str(self.duration.value))
        if duration is None:
            await interaction.response.send_message(
                "Invalid duration. Use e.g. 10m, 2h, 1d.", ephemeral=True
            )
            return
        if duration > timedelta(days=28):
            await interaction.response.send_message("Max timeout is 28d.", ephemeral=True)
            return

        reason = str(self.reason.value).strip() if self.reason.value else ""
        until = discord.utils.utcnow() + duration

        ok = 0
        failed: list[str] = []
        for user_id in self._user_ids:
            target = interaction.guild.get_member(user_id)
            if target is None:
                try:
                    target = await interaction.guild.fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    failed.append(str(user_id))
                    continue
            try:
                await target.timeout(until, reason=reason or None)
                ok += 1
            except discord.Forbidden:
                failed.append(f"{target} (forbidden)")
            except discord.HTTPException as exc:
                failed.append(f"{target} ({exc.status})")

        msg = f"Timed out {ok} user(s)."
        if failed:
            msg += " Failed: " + ", ".join(failed[:6])
        await interaction.response.send_message(msg, ephemeral=True)


class IncidentView(discord.ui.View):
    def __init__(
        self,
        payload: IncidentViewPayload,
        memory_store: MemoryStore,
        view_store: ViewStore,
    ) -> None:
        super().__init__(timeout=None)
        self.payload = payload
        self.memory_store = memory_store
        self.view_store = view_store
        self.allow_post = payload.allow_post
        self.allow_actions = payload.allow_actions
        self.selected_user_ids: list[int] = []
        self.selected_message_ids: list[int] = []
        if not self.allow_post:
            self.remove_item(self.post_public)
        elif not payload.draft_replies and not (
            (payload.draft_message or "").strip() and payload.reply_targets
        ):
            setattr(self.post_public, "disabled", True)
        if not self.allow_actions:
            self.remove_item(self.delete_messages)
            self.remove_item(self.timeout_users)
            self.remove_item(self.kick_users)
            self.remove_item(self.ban_users)
            self.remove_item(self.action_taken)
        suggestions = payload.memory_suggestions or {}
        server_notes = suggestions.get("server_notes", []) if isinstance(suggestions, dict) else []
        user_notes = suggestions.get("user_notes", []) if isinstance(suggestions, dict) else []
        if not server_notes and not user_notes:
            setattr(self.save_memory, "disabled", True)

        if self.allow_actions:
            participant_options: list[discord.SelectOption] = []
            for item in payload.participants or []:
                if not isinstance(item, dict):
                    continue
                user_id = item.get("user_id")
                name = str(item.get("name") or "user")
                role = str(item.get("role") or "")
                if user_id is None:
                    continue
                try:
                    user_id_int = int(user_id)
                except (TypeError, ValueError):
                    continue
                participant_options.append(
                    discord.SelectOption(
                        label=_truncate_one_line(name, 80),
                        description=_truncate_one_line(role, 90) if role else None,
                        value=str(user_id_int),
                    )
                )
            if participant_options:
                self.add_item(
                    _ParticipantSelect(
                        options=participant_options,
                        parent=self,
                        row=1,
                    )
                )
            else:
                setattr(self.timeout_users, "disabled", True)
                setattr(self.kick_users, "disabled", True)
                setattr(self.ban_users, "disabled", True)

            evidence_options: list[discord.SelectOption] = []
            for item in payload.evidence_quotes or []:
                if not isinstance(item, dict):
                    continue
                quote = str(item.get("quote") or "")
                message_id = item.get("message_id")
                if message_id is None:
                    continue
                try:
                    message_id_int = int(message_id)
                except (TypeError, ValueError):
                    continue
                evidence_options.append(
                    discord.SelectOption(
                        label=_truncate_one_line(quote or f"message {message_id_int}", 100),
                        value=str(message_id_int),
                    )
                )
            if evidence_options:
                self.add_item(
                    _EvidenceSelect(
                        options=evidence_options,
                        parent=self,
                        row=2,
                    )
                )
            else:
                setattr(self.delete_messages, "disabled", True)

        if payload.handled:
            for item in self.children:
                try:
                    setattr(item, "disabled", True)
                except Exception:
                    pass

    async def _ensure_mod(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if is_mod(member, self.payload.mod_role_id):
            return True
        await interaction.response.send_message("Mod permissions required.", ephemeral=True)
        return False

    def _selected_members(self, guild: discord.Guild) -> list[discord.Member]:
        out: list[discord.Member] = []
        for user_id in self.selected_user_ids:
            member = guild.get_member(user_id)
            if member is not None:
                out.append(member)
        return out

    @discord.ui.button(
        label="Post reply publicly",
        style=discord.ButtonStyle.primary,
        custom_id="incident_post_public",
    )
    async def post_public(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._ensure_mod(interaction):
            return
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return
        target_channel_id = self.payload.source_channel_id
        if target_channel_id is None:
            target_channel_id = getattr(interaction.channel, "id", None)
        if target_channel_id is None:
            await interaction.response.send_message("Channel not found.", ephemeral=True)
            return
        channel = guild.get_channel(target_channel_id) or guild.get_thread(target_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(target_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Channel not found.", ephemeral=True)
            return

        # Build pings + content.
        reply_targets = self.payload.reply_targets or []
        draft_replies = self.payload.draft_replies or []

        user_ids: list[int] = []
        if draft_replies:
            for item in draft_replies:
                if not isinstance(item, dict):
                    continue
                user_id = item.get("user_id")
                if user_id is None:
                    continue
                try:
                    uid = int(user_id)
                except (TypeError, ValueError):
                    continue
                if uid not in user_ids:
                    user_ids.append(uid)
        else:
            for item in reply_targets:
                if not isinstance(item, dict):
                    continue
                user_id = item.get("user_id")
                if user_id is None:
                    continue
                try:
                    uid = int(user_id)
                except (TypeError, ValueError):
                    continue
                if uid not in user_ids:
                    user_ids.append(uid)

        lines: list[str] = []
        if draft_replies:
            for item in draft_replies:
                if not isinstance(item, dict):
                    continue
                user_id = item.get("user_id")
                if user_id is None:
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                try:
                    uid = int(user_id)
                except (TypeError, ValueError):
                    continue
                lines.append(f"<@{uid}> {text}".strip())
            content = "\n".join(lines).strip()
        else:
            prefix = " ".join(f"<@{uid}>" for uid in user_ids).strip()
            content = f"{prefix} {self.payload.draft_message}".strip() if prefix else self.payload.draft_message.strip()

        if not content:
            await interaction.response.send_message("No draft reply to post.", ephemeral=True)
            return

        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=[discord.Object(id=uid) for uid in user_ids] if user_ids else False,
            replied_user=False,
        )

        reference = None
        if len(user_ids) == 1 and reply_targets:
            for item in reply_targets:
                if not isinstance(item, dict):
                    continue
                raw_uid = item.get("user_id")
                if raw_uid is None:
                    continue
                try:
                    uid = int(raw_uid)
                except (TypeError, ValueError):
                    continue
                if uid != user_ids[0]:
                    continue
                message_id = item.get("message_id")
                try:
                    mid = int(message_id) if message_id is not None else None
                except (TypeError, ValueError):
                    mid = None
                if mid:
                    reference = channel.get_partial_message(mid)
                break

        logger.info(
            "UI post_public guild=%s src_channel=%s post_channel=%s user=%s targets=%s reply=%s",
            guild.id,
            self.payload.source_channel_id,
            getattr(channel, "id", None),
            getattr(interaction.user, "id", None),
            ",".join(str(u) for u in user_ids) if user_ids else "",
            getattr(reference, "id", None) if reference else None,
        )

        try:
            if reference is not None:
                await channel.send(
                    content=content,
                    allowed_mentions=allowed_mentions,
                    reference=reference,
                    mention_author=False,
                )
            else:
                await channel.send(
                    content=content,
                    allowed_mentions=allowed_mentions,
                    mention_author=False,
                )
        except discord.NotFound:
            # Likely an invalid/deleted reference message. Retry without reply.
            try:
                await channel.send(
                    content=content,
                    allowed_mentions=allowed_mentions,
                    mention_author=False,
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                await interaction.response.send_message(f"Post failed: {exc}", ephemeral=True)
                return
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.response.send_message(f"Post failed: {exc}", ephemeral=True)
            return

        await interaction.response.send_message("Posted reply.", ephemeral=True)

    @discord.ui.button(
        label="Save Memory",
        style=discord.ButtonStyle.secondary,
        custom_id="incident_save_memory",
    )
    async def save_memory(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._ensure_mod(interaction):
            return
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return
        logger.info(
            "UI save_memory guild=%s channel=%s user=%s",
            guild.id,
            getattr(interaction.channel, "id", None),
            getattr(interaction.user, "id", None),
        )
        suggestions = self.payload.memory_suggestions or {}
        server_notes = suggestions.get("server_notes", [])
        user_notes = suggestions.get("user_notes", [])
        saved = 0
        for note in server_notes:
            if isinstance(note, str) and note.strip():
                await self.memory_store.add_server_memory(guild.id, note.strip())
                saved += 1
        for note in user_notes:
            if not isinstance(note, dict):
                continue
            user_id = note.get("user_id")
            label = note.get("label")
            evidence_link = note.get("evidence_link")
            if user_id is None or not label:
                continue
            try:
                uid = int(user_id)
            except (TypeError, ValueError):
                continue
            await self.memory_store.add_user_observation(
                guild.id,
                uid,
                str(label),
                str(evidence_link) if evidence_link else None,
            )
            saved += 1
        logger.info("UI save_memory saved=%s", saved)
        await interaction.response.send_message(f"Saved {saved} memory note(s).", ephemeral=True)

    @discord.ui.button(
        label="Delete Message(s)",
        style=discord.ButtonStyle.danger,
        custom_id="incident_delete_messages",
        row=3,
    )
    async def delete_messages(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._ensure_mod(interaction):
            return
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return
        target_channel_id = self.payload.source_channel_id
        if target_channel_id is None:
            channel = interaction.channel
            target_channel_id = getattr(channel, "id", None)
        if target_channel_id is None:
            await interaction.response.send_message("Channel not found.", ephemeral=True)
            return
        channel = guild.get_channel(target_channel_id) or guild.get_thread(target_channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(target_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Channel not found.", ephemeral=True)
            return
        if not self.selected_message_ids:
            await interaction.response.send_message(
                "Select evidence message(s) first.", ephemeral=True
            )
            return
        dedup: list[int] = []
        seen: set[int] = set()
        for message_id in self.selected_message_ids:
            if message_id in seen:
                continue
            seen.add(message_id)
            dedup.append(message_id)

        async def _do_delete(confirm_interaction: discord.Interaction) -> str:
            ch = channel
            ok = 0
            failed: list[str] = []
            for message_id in dedup:
                try:
                    await ch.get_partial_message(message_id).delete()
                    ok += 1
                except discord.NotFound:
                    failed.append(f"not found: {message_id}")
                except discord.Forbidden:
                    failed.append(f"forbidden: {message_id}")
                except discord.HTTPException as exc:
                    failed.append(f"error {exc.status}: {message_id}")

            out = f"Deleted {ok} message(s)."
            if failed:
                out += " Failed: " + ", ".join(failed[:6])
            return out

        await interaction.response.send_message(
            content=f"Confirm delete {len(dedup)} message(s)? This cannot be undone.",
            ephemeral=True,
            view=_ConfirmActionView(on_confirm=_do_delete),
        )

    @discord.ui.button(
        label="Action Taken",
        style=discord.ButtonStyle.success,
        custom_id="incident_action_taken",
        row=4,
    )
    async def action_taken(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._ensure_mod(interaction):
            return
        if self.payload.handled:
            await interaction.response.send_message("Already marked handled.", ephemeral=True)
            return
        message = interaction.message
        if not message:
            await interaction.response.send_message("Message not found.", ephemeral=True)
            return
        handled_payload = IncidentViewPayload(
            draft_message=self.payload.draft_message,
            reply_targets=self.payload.reply_targets,
            draft_replies=self.payload.draft_replies,
            memory_suggestions=self.payload.memory_suggestions,
            mod_role_id=self.payload.mod_role_id,
            participants=self.payload.participants,
            evidence_quotes=self.payload.evidence_quotes,
            source_channel_id=self.payload.source_channel_id,
            allow_post=self.payload.allow_post,
            allow_actions=self.payload.allow_actions,
            handled=True,
        )
        self.payload = handled_payload
        for item in self.children:
            try:
                setattr(item, "disabled", True)
            except Exception:
                pass
        embed = message.embeds[0].copy() if message.embeds else None
        if embed is not None:
            embed.color = discord.Color.green()
            footer = embed.footer.text or ""
            who = interaction.user
            who_name = who.display_name if isinstance(who, discord.Member) else str(who)
            text = f"{footer} | " if footer else ""
            embed.set_footer(text=f"{text}Handled by {who_name}")
        record = ViewRecord(
            message_id=message.id,
            channel_id=message.channel.id,
            guild_id=interaction.guild.id if interaction.guild else 0,
            payload=handled_payload.to_dict(),
            created_at=time.time(),
        )
        await self.view_store.save_view(record)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(
        label="Timeout User(s)",
        style=discord.ButtonStyle.secondary,
        custom_id="incident_timeout_users",
        row=3,
    )
    async def timeout_users(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._ensure_mod(interaction):
            return
        if not self.selected_user_ids:
            await interaction.response.send_message("Select user(s) first.", ephemeral=True)
            return
        await interaction.response.send_modal(
            _TimeoutModal(user_ids=list(self.selected_user_ids), mod_role_id=self.payload.mod_role_id)
        )

    @discord.ui.button(
        label="Kick User(s)",
        style=discord.ButtonStyle.danger,
        custom_id="incident_kick_users",
        row=3,
    )
    async def kick_users(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._ensure_mod(interaction):
            return
        if not interaction.guild:
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return
        if not self.selected_user_ids:
            await interaction.response.send_message("Select user(s) first.", ephemeral=True)
            return
        user_mentions = " ".join(f"<@{uid}>" for uid in self.selected_user_ids[:8])

        async def _do_kick(confirm_interaction: discord.Interaction) -> str:
            guild = confirm_interaction.guild
            if not guild:
                return "Guild not found."
            ok = 0
            failed: list[str] = []
            reason = f"Kick via /mod by {confirm_interaction.user} ({confirm_interaction.user.id})"
            for user_id in self.selected_user_ids:
                target = guild.get_member(user_id)
                if target is None:
                    try:
                        target = await guild.fetch_member(user_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        failed.append(str(user_id))
                        continue
                try:
                    await target.kick(reason=reason)
                    ok += 1
                except discord.Forbidden:
                    failed.append(f"{target} (forbidden)")
                except discord.HTTPException as exc:
                    failed.append(f"{target} ({exc.status})")
            msg = f"Kicked {ok} user(s)."
            if failed:
                msg += " Failed: " + ", ".join(failed[:6])
            return msg

        await interaction.response.send_message(
            content=f"Confirm kick {len(self.selected_user_ids)} user(s)? {user_mentions}",
            ephemeral=True,
            view=_ConfirmActionView(on_confirm=_do_kick),
        )

    @discord.ui.button(
        label="Ban User(s)",
        style=discord.ButtonStyle.danger,
        custom_id="incident_ban_users",
        row=3,
    )
    async def ban_users(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._ensure_mod(interaction):
            return
        if not interaction.guild:
            await interaction.response.send_message("Guild not found.", ephemeral=True)
            return
        if not self.selected_user_ids:
            await interaction.response.send_message("Select user(s) first.", ephemeral=True)
            return
        user_mentions = " ".join(f"<@{uid}>" for uid in self.selected_user_ids[:8])

        async def _do_ban(confirm_interaction: discord.Interaction) -> str:
            guild = confirm_interaction.guild
            if not guild:
                return "Guild not found."
            ok = 0
            failed: list[str] = []
            reason = f"Ban via /mod by {confirm_interaction.user} ({confirm_interaction.user.id})"
            for user_id in self.selected_user_ids:
                target = guild.get_member(user_id)
                if target is None:
                    try:
                        target = await guild.fetch_member(user_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        target = None
                try:
                    if target is not None:
                        await target.ban(reason=reason)
                    else:
                        await guild.ban(discord.Object(id=user_id), reason=reason)
                    ok += 1
                except discord.Forbidden:
                    failed.append(f"{user_id} (forbidden)")
                except discord.HTTPException as exc:
                    failed.append(f"{user_id} ({exc.status})")
            msg = f"Banned {ok} user(s)."
            if failed:
                msg += " Failed: " + ", ".join(failed[:6])
            return msg

        await interaction.response.send_message(
            content=f"Confirm ban {len(self.selected_user_ids)} user(s)? {user_mentions}",
            ephemeral=True,
            view=_ConfirmActionView(on_confirm=_do_ban),
        )
