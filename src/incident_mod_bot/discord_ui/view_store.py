from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite


@dataclass
class ViewRecord:
    message_id: int
    channel_id: int
    guild_id: int
    payload: dict[str, object]
    created_at: float


class ViewStore:
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

    def _require_conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("ViewStore is not connected")
        return self._conn

    async def _ensure_schema(self) -> None:
        conn = self._require_conn()
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS incident_views (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        await conn.commit()

    async def save_view(self, record: ViewRecord) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO incident_views (message_id, channel_id, guild_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                payload_json = excluded.payload_json,
                created_at = excluded.created_at
            """,
            (
                record.message_id,
                record.channel_id,
                record.guild_id,
                json.dumps(record.payload, ensure_ascii=True),
                record.created_at,
            ),
        )
        await conn.commit()

    async def load_views(self) -> list[ViewRecord]:
        conn = self._require_conn()
        cursor = await conn.execute(
            "SELECT message_id, channel_id, guild_id, payload_json, created_at FROM incident_views"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        records: list[ViewRecord] = []
        for row in rows:
            records.append(
                ViewRecord(
                    message_id=row["message_id"],
                    channel_id=row["channel_id"],
                    guild_id=row["guild_id"],
                    payload=json.loads(row["payload_json"]),
                    created_at=row["created_at"],
                )
            )
        return records

    async def delete_view(self, message_id: int) -> None:
        conn = self._require_conn()
        await conn.execute("DELETE FROM incident_views WHERE message_id = ?", (message_id,))
        await conn.commit()

    async def prune(self, ttl_s: float) -> None:
        if ttl_s <= 0:
            return
        conn = self._require_conn()
        cutoff = time.time() - ttl_s
        await conn.execute("DELETE FROM incident_views WHERE created_at < ?", (cutoff,))
        await conn.commit()
