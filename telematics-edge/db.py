import json
import logging
import os
from datetime import datetime, timezone
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
