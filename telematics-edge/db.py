import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "/data/telematics.db")


async def init_db() -> None:
    """Initialize SQLite schema and enable WAL for concurrent readers/writers."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    vehicle_id  TEXT NOT NULL,
                    event_type  TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload     TEXT NOT NULL,
                    synced      INTEGER DEFAULT 0
                );
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_synced ON events(synced);"
            )
            await db.commit()
            logger.info("Database initialized at %s with WAL mode enabled", DB_PATH)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to initialize database at %s: %s", DB_PATH, exc)


async def insert_event(vehicle_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """Persist an event in the local queue for later sync to cloud."""
    occurred_at = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps(payload)

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO events (vehicle_id, event_type, occurred_at, payload) VALUES (?, ?, ?, ?)",
                (vehicle_id, event_type, occurred_at, payload_json),
            )
            await db.commit()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to insert '%s' event into %s: %s", event_type, DB_PATH, exc)


async def get_unsynced_events(limit: int = 50) -> list[tuple[int, str, str]]:
    """Retrieve a batch of unsynced events from the local database, oldest first."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, event_type, payload FROM events WHERE synced = 0 ORDER BY occurred_at ASC LIMIT ?",
                (limit,),
            ) as cursor:
                return await cursor.fetchall()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to fetch unsynced events: %s", exc)
        return []


async def mark_event_synced(event_id: int) -> None:
    """Mark a specific event as successfully synced to the cloud."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE events SET synced = 1 WHERE id = ?", (event_id,))
            await db.commit()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to mark event %s as synced: %s", event_id, exc)


async def get_db_stats() -> dict[str, int]:
    """Return counts of pending and synced events for health monitoring."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT synced, COUNT(*) FROM events GROUP BY synced"
            ) as cursor:
                rows = await cursor.fetchall()
        stats: dict[str, int] = {"pending": 0, "synced": 0}
        for synced_flag, count in rows:
            if synced_flag == 0:
                stats["pending"] = count
            else:
                stats["synced"] = count
        return stats
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to fetch DB stats: %s", exc)
        return {"pending": -1, "synced": -1}


async def purge_old_synced_events(days: int = 7) -> int:
    """Delete synced events older than `days` days. Returns the number of rows deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "DELETE FROM events WHERE synced = 1 AND occurred_at < ?",
                (cutoff,),
            )
            await db.commit()
            deleted = cursor.rowcount
            if deleted:
                logger.info("Purged %d old synced events (older than %d days)", deleted, days)
            return deleted
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to purge old synced events: %s", exc)
        return 0
