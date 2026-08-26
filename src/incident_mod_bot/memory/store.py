from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import aiosqlite


class MemoryStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._ensure_schema()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _ensure_schema(self) -> None:
        conn = self._require_conn()
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                rules_channel_id INTEGER,
                mod_role_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS rules_memory (
                guild_id INTEGER PRIMARY KEY,
                content TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS server_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                note TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                evidence_link TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_profile_entries (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                count INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id, label)
            );

            CREATE TABLE IF NOT EXISTS enforcement_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER,
                brief_message_id INTEGER,
                mod_user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_user_ids TEXT NOT NULL,
                target_message_ids TEXT NOT NULL,
                rule_ids TEXT NOT NULL,
                recommended TEXT NOT NULL,
                outcome TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auto_mod_config (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL,
                default_channel_id INTEGER,
                exempt_suffix TEXT,
                cooldown_s INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auto_mod_routes (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, role_id)
            );

            CREATE TABLE IF NOT EXISTS auto_mod_ignored_categories (
                guild_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, category_id)
            );

            CREATE TABLE IF NOT EXISTS incident_payloads (
                brief_message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        await conn.commit()

    async def save_incident_payload(
        self, brief_message_id: int, guild_id: int, payload: dict[str, Any]
    ) -> None:
        """Snapshot of what analyze_incident actually saw, for replay.py.

        Channel history is not a durable record - the messages a brief was
        built from are often gone by the time anyone wants to re-check the
        wording, because deleting them is usually the enforcement action
        itself. This is the payload, not the result: replaying it re-runs the
        real prompt instead of re-describing what it produced.
        """
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO incident_payloads (brief_message_id, guild_id, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(brief_message_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                created_at = excluded.created_at
            """,
            (brief_message_id, guild_id, json.dumps(payload, ensure_ascii=True), int(time.time())),
        )
        await conn.commit()

    async def get_incident_payload(self, brief_message_id: int) -> tuple[int, dict[str, Any]] | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT guild_id, payload_json FROM incident_payloads WHERE brief_message_id = ?",
            (brief_message_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        try:
            return int(row["guild_id"]), json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None

    async def get_auto_mod_config(self, guild_id: int) -> dict[str, object]:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT enabled, default_channel_id, exempt_suffix, cooldown_s
            FROM auto_mod_config
            WHERE guild_id = ?
            """,
            (guild_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return {
                "enabled": False,
                "default_channel_id": None,
                "exempt_suffix": "-news",
                "cooldown_s": 180,
            }
        return {
            "enabled": bool(row["enabled"]),
            "default_channel_id": row["default_channel_id"],
            "exempt_suffix": row["exempt_suffix"] or "-news",
            "cooldown_s": int(row["cooldown_s"]),
        }

    async def set_auto_mod_config(
        self,
        guild_id: int,
        *,
        enabled: bool | None = None,
        default_channel_id: int | None = None,
        exempt_suffix: str | None = None,
        cooldown_s: int | None = None,
    ) -> None:
        current = await self.get_auto_mod_config(guild_id)
        use_enabled = bool(current["enabled"]) if enabled is None else bool(enabled)
        use_default = current["default_channel_id"] if default_channel_id is None else default_channel_id
        use_suffix = str(current["exempt_suffix"] or "-news") if exempt_suffix is None else exempt_suffix
        use_cooldown = int(current["cooldown_s"]) if cooldown_s is None else int(cooldown_s)
        now = int(time.time())
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO auto_mod_config (guild_id, enabled, default_channel_id, exempt_suffix, cooldown_s, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                enabled = excluded.enabled,
                default_channel_id = excluded.default_channel_id,
                exempt_suffix = excluded.exempt_suffix,
                cooldown_s = excluded.cooldown_s,
                updated_at = excluded.updated_at
            """,
            (
                guild_id,
                1 if use_enabled else 0,
                use_default,
                use_suffix,
                use_cooldown,
                now,
            ),
        )
        await conn.commit()

    async def set_auto_mod_route(self, guild_id: int, role_id: int, channel_id: int) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO auto_mod_routes (guild_id, role_id, channel_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, role_id) DO UPDATE SET
                channel_id = excluded.channel_id
            """,
            (guild_id, role_id, channel_id),
        )
        await conn.commit()

    async def delete_auto_mod_route(self, guild_id: int, role_id: int) -> None:
        conn = self._require_conn()
        await conn.execute(
            "DELETE FROM auto_mod_routes WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        await conn.commit()

    async def list_auto_mod_routes(self, guild_id: int) -> list[tuple[int, int]]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT role_id, channel_id FROM auto_mod_routes WHERE guild_id = ?",
            (guild_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [(int(row["role_id"]), int(row["channel_id"])) for row in rows]

    async def add_auto_mod_ignored_category(self, guild_id: int, category_id: int) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO auto_mod_ignored_categories (guild_id, category_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id, category_id) DO NOTHING
            """,
            (guild_id, category_id),
        )
        await conn.commit()

    async def delete_auto_mod_ignored_category(self, guild_id: int, category_id: int) -> None:
        conn = self._require_conn()
        await conn.execute(
            "DELETE FROM auto_mod_ignored_categories WHERE guild_id = ? AND category_id = ?",
            (guild_id, category_id),
        )
        await conn.commit()

    async def list_auto_mod_ignored_categories(self, guild_id: int) -> list[int]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT category_id FROM auto_mod_ignored_categories WHERE guild_id = ?",
            (guild_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [int(row["category_id"]) for row in rows]

    def _require_conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("MemoryStore is not connected")
        return self._conn

    async def get_guild_config(self, guild_id: int) -> dict[str, int | None]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT rules_channel_id, mod_role_id FROM guild_config WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return {"rules_channel_id": None, "mod_role_id": None}
        return {"rules_channel_id": row["rules_channel_id"], "mod_role_id": row["mod_role_id"]}

    async def set_guild_config(
        self,
        guild_id: int,
        rules_channel_id: int | None = None,
        mod_role_id: int | None = None,
    ) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO guild_config (guild_id, rules_channel_id, mod_role_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                rules_channel_id = COALESCE(excluded.rules_channel_id, rules_channel_id),
                mod_role_id = COALESCE(excluded.mod_role_id, mod_role_id)
            """,
            (guild_id, rules_channel_id, mod_role_id),
        )
        await conn.commit()

    async def get_rules_memory(self, guild_id: int) -> str | None:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT content FROM rules_memory WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return None if not row else row["content"]

    async def set_rules_memory(self, guild_id: int, content: str) -> None:
        conn = self._require_conn()
        now = int(time.time())
        await conn.execute(
            """
            INSERT INTO rules_memory (guild_id, content, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                content = excluded.content,
                updated_at = excluded.updated_at
            """,
            (guild_id, content, now),
        )
        await conn.commit()

    async def add_server_memory(self, guild_id: int, note: str) -> None:
        conn = self._require_conn()
        now = int(time.time())
        await conn.execute(
            "INSERT INTO server_memory (guild_id, note, created_at) VALUES (?, ?, ?)",
            (guild_id, note, now),
        )
        await conn.commit()

    async def list_server_memory(self, guild_id: int, limit: int = 5) -> list[str]:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT note FROM server_memory
            WHERE guild_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [row["note"] for row in rows]

    async def add_enforcement_entry(
        self,
        *,
        guild_id: int,
        channel_id: int | None,
        brief_message_id: int | None,
        mod_user_id: int,
        action: str,
        target_user_ids: list[int],
        target_message_ids: list[int],
        rule_ids: list[str],
        recommended: list[str],
        outcome: str | None,
    ) -> None:
        """Record what a moderator actually did. Written automatically on button press."""
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO enforcement_log (
                guild_id, channel_id, brief_message_id, mod_user_id, action,
                target_user_ids, target_message_ids, rule_ids, recommended,
                outcome, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id, channel_id, brief_message_id, mod_user_id, action,
                json.dumps(list(target_user_ids)), json.dumps(list(target_message_ids)),
                json.dumps(list(rule_ids)), json.dumps(list(recommended)),
                outcome, int(time.time()),
            ),
        )
        await conn.commit()

    async def list_enforcement_entries(
        self, guild_id: int, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT * FROM enforcement_log WHERE guild_id = ?
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            (guild_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key in ("target_user_ids", "target_message_ids", "rule_ids", "recommended"):
                try:
                    item[key] = json.loads(item[key])
                except (TypeError, ValueError):
                    item[key] = []
            out.append(item)
        return out

    async def summarize_enforcement(self, guild_id: int, *, limit: int = 200) -> list[str]:
        """Aggregate enforcement norms for the model.

        Deliberately contains no user ids or names: the model gets how this
        server enforces its rules, never opinions about individuals.
        """
        entries = await self.list_enforcement_entries(guild_id, limit=limit)
        if not entries:
            return []

        by_rule: dict[str, Counter] = defaultdict(Counter)
        overall: Counter = Counter()
        for e in entries:
            overall[e["action"]] += 1
            for rule in e["rule_ids"] or ["(unspecified)"]:
                by_rule[rule][e["action"]] += 1

        notes: list[str] = []
        for rule, actions in sorted(by_rule.items(), key=lambda kv: -sum(kv[1].values())):
            total = sum(actions.values())
            if total < 2:
                continue
            top, count = actions.most_common(1)[0]
            notes.append(f"{rule}: usually {top} ({count} of {total}).")
        if not notes:
            top, count = overall.most_common(1)[0]
            notes.append(f"Most common moderator action: {top} ({count}).")
        return notes[:5]

    async def enforcement_stats(self, guild_id: int, *, limit: int = 500) -> dict[str, Any]:
        """Headline counts for the /enforcement view."""
        entries = await self.list_enforcement_entries(guild_id, limit=limit)
        by_action: Counter = Counter(e["action"] for e in entries)
        return {
            "total": len(entries),
            "by_action": dict(by_action),
            "moderators": len({e["mod_user_id"] for e in entries}),
            "latest": entries[0]["created_at"] if entries else None,
        }

    async def enforcement_divergence(self, guild_id: int, *, limit: int = 200) -> list[str]:
        """Where the bot's advice and the moderators' choices disagree.

        The only direct measure of whether the recommendations are any good.
        """
        entries = await self.list_enforcement_entries(guild_id, limit=limit)
        gaps: Counter = Counter()
        for e in entries:
            advised = " ".join(e["recommended"]).lower()
            if not advised:
                continue
            for verb in ("timeout", "ban", "kick", "delete", "warn"):
                if verb in advised and verb not in e["action"].lower():
                    gaps[verb] += 1
        return [
            f"Recommended '{verb}' {n} time(s) where the moderator did something else."
            for verb, n in gaps.most_common(3)
            if n >= 2
        ]

    async def add_user_observation(
        self,
        guild_id: int,
        user_id: int,
        label: str,
        evidence_link: str | None,
    ) -> None:
        conn = self._require_conn()
        now = int(time.time())
        await conn.execute(
            """
            INSERT INTO user_observations (guild_id, user_id, label, evidence_link, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, label, evidence_link, now),
        )
        await conn.execute(
            """
            INSERT INTO user_profile_entries (guild_id, user_id, label, count, last_seen)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(guild_id, user_id, label) DO UPDATE SET
                count = count + 1,
                last_seen = excluded.last_seen
            """,
            (guild_id, user_id, label, now),
        )
        await conn.commit()

    async def list_user_profile_entries(
        self,
        guild_id: int,
        user_id: int,
        min_count: int = 2,
        limit: int = 5,
    ) -> list[tuple[str, int]]:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT label, count FROM user_profile_entries
            WHERE guild_id = ? AND user_id = ? AND count >= ?
            ORDER BY count DESC, last_seen DESC
            LIMIT ?
            """,
            (guild_id, user_id, min_count, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [(row["label"], row["count"]) for row in rows]

    async def delete_server_memory(self, guild_id: int) -> None:
        conn = self._require_conn()
        await conn.execute("DELETE FROM server_memory WHERE guild_id = ?", (guild_id,))
        await conn.commit()

    async def delete_user_memory(self, guild_id: int) -> None:
        conn = self._require_conn()
        await conn.execute("DELETE FROM user_observations WHERE guild_id = ?", (guild_id,))
        await conn.execute("DELETE FROM user_profile_entries WHERE guild_id = ?", (guild_id,))
        await conn.commit()

    async def list_observations(
        self,
        guild_id: int,
        limit: int = 25,
    ) -> list[tuple[int, int, str, str | None]]:
        conn = self._require_conn()
        cursor = await conn.execute(
            """
            SELECT user_id, id, label, evidence_link
            FROM user_observations
            WHERE guild_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [(row["user_id"], row["id"], row["label"], row["evidence_link"]) for row in rows]

    @staticmethod
    def format_profile(entries: Iterable[tuple[str, int]]) -> str:
        lines = [f"- {label} (seen {count}x)" for label, count in entries]
        return "\n".join(lines)


def format_enforcement_report(
    *,
    norms: list[str],
    divergence: list[str],
    stats: dict[str, Any],
) -> str:
    """Render the ledger for humans.

    Deliberately aggregate: this is about rules and enforcement, never about
    individuals, so no user ids appear here.
    """
    if not stats.get("total"):
        return (
            "**Enforcement history**\n"
            "No enforcement history yet. It builds automatically as moderators "
            "use the buttons on incident briefs - nothing to set up."
        )

    lines = ["**Enforcement history**", ""]
    by_action = stats.get("by_action") or {}
    breakdown = ", ".join(
        f"{action} x{count}"
        for action, count in sorted(by_action.items(), key=lambda kv: -kv[1])
    )
    lines.append(
        f"{stats['total']} recorded action(s) by {stats.get('moderators', 0)} moderator(s)."
    )
    if breakdown:
        lines.append(f"Breakdown: {breakdown}")

    if norms:
        lines += ["", "__How this server enforces its rules__"]
        lines += [f"- {n}" for n in norms]

    if divergence:
        lines += ["", "__Where the bot's advice and moderators disagree__"]
        lines += [f"- {d}" for d in divergence]
        lines.append("")
        lines.append("_Large gaps here usually mean the recommendations need tuning._")

    return "\n".join(lines)
