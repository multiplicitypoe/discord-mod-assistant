from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from incident_mod_bot.discord_ui.view_store import ViewRecord, ViewStore
from incident_mod_bot.memory.store import MemoryStore
from incident_mod_bot.utils.discord import is_mod

logger = logging.getLogger("incident_mod_bot")

# How far back to look for moderation done outside the brief, and how much of
# the audit log to walk. Most incidents are closed inside an hour or two, so a
# wider window mostly adds unrelated actions rather than missing ones.
_AUDIT_LOOKBACK_S = 2 * 3600
_AUDIT_SCAN_LIMIT = 200
_AUDIT_MAX_LINES = 8

# Moderators often press Action Taken and then go and do the thing, so a single
# look at the moment of the press is usually too early. Wait these many seconds
# between looks, giving up after the last one.
_AUDIT_FOLLOW_UP_S = (30, 120, 300, 900)

# A channel reply is a weaker signal than a real action - a moderator can say
# "on it" before actually doing the thing. Showing the reply right away and
# then never looking again would let a later action get permanently
# stranded off the card. So once the normal schedule ends with only a reply
# to show, keep checking the audit log alone - no need to rescan the
# channel, the reply is already shown - a couple more times, out to roughly
# the same "hour or two" horizon the lookback itself already assumes.
_REPLY_EXTRA_FOLLOW_UP_S = (1800, 3600)

_ACTION_FIELD = "Action taken:"

# Background work has to be held onto. The event loop keeps only weak
# references to tasks, so one whose only reference is a local variable can be
# collected part way through and cancelled without a word. That is exactly how
# the action summary managed to never once appear.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _spawn(coro: Any, what: str) -> asyncio.Task[Any]:
    """Run something after the interaction has been answered, and keep it alive."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)

    def _done(finished: asyncio.Task[Any]) -> None:
        _BACKGROUND_TASKS.discard(finished)
        if finished.cancelled():
            logger.warning("Background work was cancelled: %s", what)
            return
        error = finished.exception()
        if error is not None:
            logger.error("Background work failed: %s: %r", what, error)

    task.add_done_callback(_done)
    return task


def _humanise_until(start, until) -> str:
    """Render a timeout as a duration rather than a timestamp."""
    try:
        seconds = max(0, int((until - start).total_seconds()))
    except Exception:
        return "for a while"
    if seconds >= 86400:
        days = round(seconds / 86400)
        return f"for {days}d"
    if seconds >= 3600:
        return f"for {round(seconds / 3600)}h"
    if seconds >= 60:
        return f"for {round(seconds / 60)}m"
    return f"for {seconds}s"


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
    # What the brief advised, carried so the ledger can compare advice to action.
    recommendations: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    # Destructive tools stay collapsed until asked for. Moderators overwhelmingly
    # read the brief and press Action Taken; Ban sitting next to it is a mis-tap
    # waiting to happen on mobile.
    expanded: bool = False
    # Bump when component custom_id layout changes.
    view_version: int = 3

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
            "recommendations": self.recommendations,
            "rule_ids": self.rule_ids,
            "expanded": self.expanded,
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
    def __init__(
        self,
        *,
        user_ids: list[int],
        mod_role_id: int | None,
        on_applied: Any = None,
    ) -> None:
        super().__init__(title="Timeout Users")
        self._user_ids = user_ids
        self._mod_role_id = mod_role_id
        # Called with (interaction, outcome) once timeouts are applied, so the
        # enforcement ledger can record it. The modal has no store of its own.
        self._on_applied = on_applied
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
        if self._on_applied is not None:
            try:
                await self._on_applied(interaction, msg)
            except Exception:
                logger.exception("timeout ledger callback failed")
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
            self.remove_item(self.expand_actions)
        elif not payload.expanded:
            # Collapsed by default: only what is actually used stays on screen.
            for item in (
                self.delete_messages, self.timeout_users,
                self.kick_users, self.ban_users, self.save_memory,
            ):
                self.remove_item(item)
        else:
            self.remove_item(self.expand_actions)
        suggestions = payload.memory_suggestions or {}
        server_notes = suggestions.get("server_notes", []) if isinstance(suggestions, dict) else []
        user_notes = suggestions.get("user_notes", []) if isinstance(suggestions, dict) else []
        if not server_notes and not user_notes:
            setattr(self.save_memory, "disabled", True)

        if self.allow_actions and payload.expanded:
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
            # Strip the controls rather than disabling them, the same as
            # pressing Action Taken does. Disabling them left every restored
            # card sitting under two rows of dead buttons.
            for item in list(self.children):
                try:
                    self.remove_item(item)
                except Exception:
                    logger.debug("Could not remove %r from a handled brief", item)

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

    async def _attach_action_summary(self, interaction: Any, message: Any) -> None:
        """Add what the audit log says was done, after the button has responded.

        Runs in the background because reading the audit log can take longer
        than Discord's three second interaction deadline, and a slow read must
        never make the button look broken.

        It looks again at 30s, 2m, 7m and 22m after the press, because
        pressing the button and then going to do the thing is the normal order.
        The window always reaches back the full lookback from the press, so a
        later look never slides forward off an action it already had in range.

        If all that schedule ever turns up is a channel reply, two more looks
        follow at +30m and +60m, audit log only, in case a real action was
        still coming - see _REPLY_EXTRA_FOLLOW_UP_S.
        """
        brief = getattr(message, "id", "?")
        since = datetime.now(timezone.utc) - timedelta(seconds=_AUDIT_LOOKBACK_S)
        shown: list[str] = []
        shown_is_reply = False
        try:
            for wait_s in (0,) + tuple(_AUDIT_FOLLOW_UP_S):
                if wait_s:
                    await asyncio.sleep(wait_s)
                lines = await self._collect_recent_mod_actions(interaction, since=since)
                is_reply = False
                if not lines:
                    # No audit-log action doesn't mean nothing happened - often
                    # a moderator just answered in the channel. Only checked as
                    # a fallback: a real action already tells the fuller story.
                    lines = await self._collect_recent_mod_channel_replies(
                        interaction, since=since
                    )
                    is_reply = bool(lines)
                if not lines or lines == shown:
                    continue
                shown = lines
                shown_is_reply = is_reply
                await self._show_action_summary(message, shown)
                logger.info("Action summary for brief %s: %s", brief, "; ".join(shown))
                if self.memory_store is not None:
                    await self._log_enforcement(
                        interaction,
                        "audit_summary",
                        outcome="; ".join(shown)[:500],
                    )

            # A reply is provisional: a moderator can say "on it" before doing
            # the thing. Don't stop watching just because the normal schedule
            # ran out - keep checking the audit log alone, further apart, and
            # let a real action overwrite the reply whenever it shows up.
            if shown_is_reply:
                for wait_s in _REPLY_EXTRA_FOLLOW_UP_S:
                    await asyncio.sleep(wait_s)
                    lines = await self._collect_recent_mod_actions(interaction, since=since)
                    if not lines or lines == shown:
                        continue
                    shown = lines
                    await self._show_action_summary(message, shown)
                    logger.info(
                        "Action summary for brief %s upgraded past a channel reply: %s",
                        brief, "; ".join(shown),
                    )
                    if self.memory_store is not None:
                        await self._log_enforcement(
                            interaction,
                            "audit_summary",
                            outcome="; ".join(shown)[:500],
                        )
                    break

            if not shown:
                # Deliberately nothing on the card. An empty field reads as a
                # broken bot, so the record of having looked goes to the log.
                logger.info(
                    "Action summary for brief %s: audit log showed nothing for "
                    "these people after %d looks", brief, len(_AUDIT_FOLLOW_UP_S) + 1)
        except asyncio.CancelledError:
            logger.warning("Action summary for brief %s was cancelled", brief)
            raise
        except Exception:
            logger.exception("Failed to attach the action summary")

    async def _show_action_summary(self, message: Any, lines: list[str]) -> None:
        """Put the findings on the card.

        Replaces the field rather than adding another one, since a later look
        finding more would otherwise leave two copies stacked up.
        """
        fresh = await message.channel.fetch_message(message.id)
        embed = fresh.embeds[0].copy() if fresh.embeds else None
        if embed is None:
            return
        body = "\n".join(f"\u2022 {line}" for line in lines)[:1024]
        for index, existing in enumerate(embed.fields):
            if existing.name == _ACTION_FIELD:
                embed.set_field_at(index, name=_ACTION_FIELD, value=body, inline=False)
                break
        else:
            embed.add_field(name=_ACTION_FIELD, value=body, inline=False)
        await fresh.edit(embed=embed, view=self)

    async def _collect_recent_mod_actions(
        self, interaction: Any, since: datetime | None = None
    ) -> list[str]:
        """Summarise audit log moderation against the people in this brief.

        Requires View Audit Log. Returns [] when the permission is missing or
        anything goes wrong: this is decoration on a record, never a reason to
        fail the button.
        """
        guild = getattr(interaction, "guild", None)
        if guild is None:
            return []
        # payload.participants is the persisted view payload, plain dicts (see
        # the participant select above), not the pydantic Participant objects
        # result.participants holds. Attribute access here silently matched
        # nothing, so every audit log lookup ran against an empty id set
        # unless a moderator had used the participant dropdown themselves.
        participant_ids: set[int] = set()
        names: dict[int, str] = {}
        for p in self.payload.participants or []:
            if not isinstance(p, dict):
                continue
            raw_id = p.get("user_id")
            if raw_id is None:
                continue
            try:
                pid = int(raw_id)
            except (TypeError, ValueError):
                continue
            participant_ids.add(pid)
            names[pid] = str(p.get("name") or pid)
        participant_ids.update(int(u) for u in self.selected_user_ids)
        after = since or (datetime.now(timezone.utc) - timedelta(seconds=_AUDIT_LOOKBACK_S))
        found: list[str] = []
        seen: set[tuple] = set()
        try:
            entries = [e async for e in guild.audit_logs(limit=_AUDIT_SCAN_LIMIT, after=after)]

            # Someone can be acted on without ever reaching the brief, which is
            # exactly what happens when the bot could not read what they posted.
            # A message of theirs being deleted from this channel is enough to
            # bring them into scope, and then their timeout or ban counts too.
            source_channel_id = self.payload.source_channel_id
            for entry in entries:
                if entry.action not in (
                    discord.AuditLogAction.message_delete,
                    discord.AuditLogAction.message_bulk_delete,
                ):
                    continue
                where = getattr(getattr(entry, "extra", None), "channel", None)
                where_id = getattr(where, "id", where)
                if not source_channel_id or not where_id:
                    continue
                if int(where_id) != int(source_channel_id):
                    continue
                raw = getattr(getattr(entry, "target", None), "id", None)
                try:
                    participant_ids.add(int(raw))
                except (TypeError, ValueError):
                    continue

            if not participant_ids:
                return []

            for entry in entries:
                # Not every audit target is a user. Invite entries carry a code
                # such as 'AyewUQ6G' as their id, and an unguarded int() there
                # aborted the whole scan, so the summary silently came back
                # empty whenever an invite appeared in the window.
                raw_target = getattr(getattr(entry, "target", None), "id", None)
                try:
                    target_id = int(raw_target)
                except (TypeError, ValueError):
                    continue
                if target_id not in participant_ids:
                    continue
                who = (
                    getattr(entry.user, "display_name", None)
                    or getattr(entry.user, "name", None)
                    or "unknown"
                )
                # Someone pulled in from the audit log alone has no entry in the
                # brief, so take whatever name the log itself carries.
                target_obj = getattr(entry, "target", None)
                subject = names.get(target_id) or (
                    getattr(target_obj, "display_name", None)
                    or getattr(target_obj, "name", None)
                    or str(target_id)
                )
                action = entry.action
                line = None
                if action is discord.AuditLogAction.member_update:
                    until = getattr(getattr(entry, "after", None), "timed_out_until", None)
                    if until is not None:
                        line = f"Timed out {subject} {_humanise_until(entry.created_at, until)}"
                elif action is discord.AuditLogAction.kick:
                    line = f"Kicked {subject}"
                elif action is discord.AuditLogAction.ban:
                    line = f"Banned {subject}"
                elif action is discord.AuditLogAction.unban:
                    line = f"Unbanned {subject}"
                elif action in (
                    discord.AuditLogAction.message_delete,
                    discord.AuditLogAction.message_bulk_delete,
                ):
                    # Only deletions in the channel this incident came from. A
                    # participant having a message removed somewhere else in the
                    # server is unrelated and would be misleading here.
                    # Note Discord only logs moderator deletions, so a user
                    # removing their own message never appears.
                    where = getattr(getattr(entry, "extra", None), "channel", None)
                    where_id = getattr(where, "id", where)
                    source = self.payload.source_channel_id
                    if source and where_id and int(where_id) != int(source):
                        continue
                    count = getattr(getattr(entry, "extra", None), "count", None)
                    n = f"{count} messages" if count else "a message"
                    line = f"Deleted {n} from {subject}"
                if not line:
                    continue
                key = (line, who)
                if key in seen:
                    continue
                seen.add(key)
                found.append(f"{line} \u00b7 by {who}")
                if len(found) >= _AUDIT_MAX_LINES:
                    break
        except discord.Forbidden:
            logger.info("No View Audit Log permission; skipping action summary")
            return []
        except Exception:
            logger.exception("Failed to read audit log for action summary")
            return []
        return found

    async def _collect_recent_mod_channel_replies(
        self, interaction: Any, since: datetime | None = None
    ) -> list[str]:
        """A moderator answering in the channel, when there's nothing to
        enforce and so nothing in the audit log.

        A message only earns a first look by being an actual Discord reply to
        the brief's own ping or evidence - not just a moderator being active
        in the channel afterward, which in a busy channel would be almost
        every message. But a moderator who has replied once is now part of
        the incident's own conversation, and real answers routinely spill
        into a second message ("do you think I should forward the GGG
        member's message?") that isn't itself a reply to anything. Once a
        moderator clears the reply bar, their later messages in the same
        window count too.
        """
        guild = getattr(interaction, "guild", None)
        source_channel_id = self.payload.source_channel_id
        if guild is None or not source_channel_id:
            return []

        relevant_ids: set[int] = set()
        for t in (self.payload.reply_targets or []) + (self.payload.evidence_quotes or []):
            if not isinstance(t, dict):
                continue
            raw = t.get("message_id")
            if raw is None:
                continue
            try:
                relevant_ids.add(int(raw))
            except (TypeError, ValueError):
                continue
        if not relevant_ids:
            return []

        after = since or (datetime.now(timezone.utc) - timedelta(seconds=_AUDIT_LOOKBACK_S))

        def _is_mod_author(msg: Any) -> bool:
            try:
                return not getattr(msg.author, "bot", False) and is_mod(
                    msg.author, self.payload.mod_role_id
                )
            except AttributeError:
                # A guild channel's history hands back Member objects, not
                # bare Users, so this has roles and guild_permissions to
                # check - but one odd author must never abort the whole scan.
                return False

        def _line(msg: Any, *, is_reply: bool) -> str:
            who = (
                getattr(msg.author, "display_name", None)
                or getattr(msg.author, "name", None)
                or "unknown"
            )
            content = _truncate_one_line(msg.clean_content, 140)
            verb = "Replied in channel" if is_reply else "Also in channel"
            return f'{verb}: "{content}" · by {who}'

        found: list[str] = []
        seen: set[int] = set()
        try:
            client = getattr(interaction, "client", None)
            channel = await client.fetch_channel(source_channel_id)
            messages = [
                m async for m in channel.history(after=after, limit=_AUDIT_SCAN_LIMIT)
            ]
            messages.sort(key=lambda m: m.id)

            engaged_mod_ids: set[int] = set()
            for msg in messages:
                ref = getattr(msg, "reference", None)
                if getattr(ref, "message_id", None) not in relevant_ids:
                    continue
                if not _is_mod_author(msg):
                    continue
                seen.add(msg.id)
                engaged_mod_ids.add(msg.author.id)
                found.append(_line(msg, is_reply=True))
                if len(found) >= _AUDIT_MAX_LINES:
                    return found

            if engaged_mod_ids:
                for msg in messages:
                    if msg.id in seen or msg.author.id not in engaged_mod_ids:
                        continue
                    if not _is_mod_author(msg):
                        continue
                    seen.add(msg.id)
                    found.append(_line(msg, is_reply=False))
                    if len(found) >= _AUDIT_MAX_LINES:
                        break
        except discord.Forbidden:
            return []
        except Exception:
            logger.exception("Failed to scan the channel for moderator replies")
            return []
        return found

    async def _log_enforcement(
        self,
        interaction: Any,
        action: str,
        *,
        target_user_ids: list[int] | None = None,
        target_message_ids: list[int] | None = None,
        outcome: str | None = None,
    ) -> None:
        """Record what the moderator actually did.

        Ground truth for the enforcement ledger: written automatically on every
        button press, so no moderator effort is required. Never raises - a
        bookkeeping failure must not break moderation.
        """
        try:
            guild_id = getattr(interaction, "guild_id", None)
            if not guild_id or self.memory_store is None:
                return
            user = getattr(interaction, "user", None)
            message = getattr(interaction, "message", None)
            users = list(target_user_ids or [])
            messages = list(target_message_ids or [])
            who = getattr(user, "display_name", None) or getattr(user, "name", "?")
            logger.info(
                "ENFORCE action=%s by=%s(%s) brief=%s users=%s messages=%s "
                "rules=%s outcome=%s",
                action,
                who,
                getattr(user, "id", "?"),
                getattr(message, "id", None),
                # comma joined rather than list repr: a repr contains spaces,
                # which makes the line awkward to parse downstream
                ",".join(str(u) for u in users) or "-",
                ",".join(str(m) for m in messages) or "-",
                ",".join(self.payload.rule_ids or []) or "-",
                outcome or "-",
            )
            await self.memory_store.add_enforcement_entry(
                guild_id=int(guild_id),
                channel_id=self.payload.source_channel_id,
                brief_message_id=getattr(message, "id", None),
                mod_user_id=int(getattr(user, "id", 0) or 0),
                action=action,
                target_user_ids=users,
                target_message_ids=messages,
                rule_ids=list(self.payload.rule_ids or []),
                recommended=list(self.payload.recommendations or []),
                outcome=outcome,
            )
        except Exception:
            logger.exception("failed to record enforcement entry action=%s", action)

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

        await self._log_enforcement(
            interaction,
            "posted_reply",
            target_user_ids=[
                int(t.get("user_id"))
                for t in (self.payload.reply_targets or [])
                if str(t.get("user_id") or "").isdigit()
            ],
            outcome=(self.payload.draft_message or "").strip()[:400] or None,
        )
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
        label="Delete Messages",
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
            await self._log_enforcement(
                confirm_interaction, "deleted",
                target_message_ids=list(dedup), outcome=out,
            )
            return out

        await interaction.response.send_message(
            content=f"Confirm delete {len(dedup)} message(s)? This cannot be undone.",
            ephemeral=True,
            view=_ConfirmActionView(on_confirm=_do_delete),
        )

    @discord.ui.button(
        label="Moderate...",
        style=discord.ButtonStyle.secondary,
        custom_id="incident_expand_actions",
        row=4,
    )
    async def expand_actions(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Reveal the destructive tools on demand, in place."""
        if not await self._ensure_mod(interaction):
            return
        expanded_payload = replace(self.payload, expanded=True)
        view = IncidentView(expanded_payload, self.memory_store, self.view_store)
        await interaction.response.edit_message(view=view)

    @discord.ui.button(
        label="Mark Handled",
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
            recommendations=self.payload.recommendations,
            rule_ids=self.payload.rule_ids,
            source_channel_id=self.payload.source_channel_id,
            allow_post=self.payload.allow_post,
            allow_actions=self.payload.allow_actions,
            handled=True,
        )
        self.payload = handled_payload
        # Strip the controls rather than disabling them. A resolved report should
        # read as a short record, not a wall of dead buttons.
        for item in list(self.children):
            try:
                self.remove_item(item)
            except Exception:
                pass
        embed = message.embeds[0].copy() if message.embeds else None
        if embed is not None:
            embed.color = discord.Color.green()
            # Nobody uses it once a mod has actually replied or moved on; it's
            # dead weight on a resolved card.
            for index, existing in enumerate(embed.fields):
                if existing.name == "Draft reply":
                    embed.remove_field(index)
                    break
            footer = embed.footer.text or ""
            who = interaction.user
            who_name = who.display_name if isinstance(who, discord.Member) else str(who)
            text = f"{footer} | " if footer else ""
            embed.set_footer(text=f"{text}Marked Handled by {who_name}")
        record = ViewRecord(
            message_id=message.id,
            channel_id=message.channel.id,
            guild_id=interaction.guild.id if interaction.guild else 0,
            payload=handled_payload.to_dict(),
            created_at=time.time(),
        )
        await self.view_store.save_view(record)
        await self._log_enforcement(
            interaction,
            "action_taken",
            target_user_ids=list(self.selected_user_ids),
            target_message_ids=list(self.selected_message_ids),
            outcome="handled_externally" if not self.selected_user_ids else None,
        )
        await interaction.response.edit_message(embed=embed, view=self)
        # Whoever actually acted may not be whoever pressed the button, and they
        # often act in Discord directly, so ask the audit log rather than assume.
        _spawn(self._attach_action_summary(interaction, message),
               f"action summary for brief {message.id}")

    @discord.ui.button(
        label="Timeout Users",
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
            _TimeoutModal(
                user_ids=list(self.selected_user_ids),
                mod_role_id=self.payload.mod_role_id,
                on_applied=lambda modal_interaction, outcome: self._log_enforcement(
                    modal_interaction,
                    "timeout",
                    target_user_ids=list(self.selected_user_ids),
                    outcome=outcome,
                ),
            )
        )

    @discord.ui.button(
        label="Kick Users",
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
            await self._log_enforcement(
                confirm_interaction, "kick",
                target_user_ids=list(self.selected_user_ids), outcome=msg,
            )
            return msg

        await interaction.response.send_message(
            content=f"Confirm kick {len(self.selected_user_ids)} user(s)? {user_mentions}",
            ephemeral=True,
            view=_ConfirmActionView(on_confirm=_do_kick),
        )

    @discord.ui.button(
        label="Ban Users",
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
            await self._log_enforcement(
                confirm_interaction, "ban",
                target_user_ids=list(self.selected_user_ids), outcome=msg,
            )
            return msg

        await interaction.response.send_message(
            content=f"Confirm ban {len(self.selected_user_ids)} user(s)? {user_mentions}",
            ephemeral=True,
            view=_ConfirmActionView(on_confirm=_do_ban),
        )
