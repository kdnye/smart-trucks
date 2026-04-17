import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "/data/telematics.db")
SQLITE_CONNECT_TIMEOUT_SECONDS = float(os.getenv("SQLITE_CONNECT_TIMEOUT_SECONDS", "30"))
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "30000"))

_db_connection: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()

async def _configure_sqlite_connection(conn: aiosqlite.Connection) -> None:
    """Configure SQLite for cross-process writer contention on shared edge storage."""
    await conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
    journal_mode_cursor = await conn.execute("PRAGMA journal_mode=WAL;")
    journal_mode_row = await journal_mode_cursor.fetchone()
    await conn.execute("PRAGMA synchronous=NORMAL;")
    await conn.execute("PRAGMA temp_store=MEMORY;")

    if journal_mode_row and str(journal_mode_row[0]).lower() != "wal":
        logger.warning("SQLite journal mode is %s (expected WAL)", journal_mode_row[0])


async def _get_db_connection() -> aiosqlite.Connection:
    """Return a shared SQLite connection to avoid thread churn on constrained devices."""
    global _db_connection

    if _db_connection is not None:
        return _db_connection

    async with _db_lock:
        if _db_connection is None:
            _db_connection = await aiosqlite.connect(
                DB_PATH,
                timeout=SQLITE_CONNECT_TIMEOUT_SECONDS,
                isolation_level="IMMEDIATE",
            )
            await _configure_sqlite_connection(_db_connection)
        return _db_connection


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    """Initialize SQLite schema and enable WAL for concurrent readers/writers."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    try:
        db = await _get_db_connection()
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS gps_points (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_id        TEXT NOT NULL,
                captured_at_utc   TEXT NOT NULL,
                lat               REAL,
                lon               REAL,
                speed_kmh         REAL,
                fix_status        TEXT NOT NULL,
                source_device     TEXT,
                trip_id           TEXT,
                local_sequence    INTEGER NOT NULL,
                sent_at_utc       TEXT,
                attempt_count     INTEGER NOT NULL DEFAULT 0,
                payload_json      TEXT NOT NULL
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS heartbeats (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_id        TEXT NOT NULL,
                heartbeat_type    TEXT NOT NULL,
                captured_at_utc   TEXT NOT NULL,
                payload_json      TEXT NOT NULL,
                sent_at_utc       TEXT,
                attempt_count     INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS edge_health (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at_utc          TEXT NOT NULL,
                last_gps_fix_utc         TEXT,
                last_upload_success_utc  TEXT,
                queue_depth              INTEGER NOT NULL,
                disk_free_mb             REAL,
                wifi_state               TEXT,
                process_state            TEXT,
                payload_json             TEXT NOT NULL
            );
            """
        )

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_gps_points_pending ON gps_points(sent_at_utc, captured_at_utc);"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_heartbeats_pending ON heartbeats(sent_at_utc, captured_at_utc);"
        )
        await db.commit()
        logger.info("Database initialized at %s with WAL mode enabled", DB_PATH)
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to initialize database at %s: %s", DB_PATH, exc)
        raise


async def insert_gps_point(
    *,
    vehicle_id: str,
    captured_at_utc: str,
    lat: float | None,
    lon: float | None,
    speed_kmh: float | None,
    fix_status: str,
    source_device: str | None,
    trip_id: str | None,
    local_sequence: int,
    payload: dict[str, Any],
) -> None:
    payload_json = json.dumps(payload)

    try:
        db = await _get_db_connection()
        await db.execute(
            """
            INSERT INTO gps_points (
                vehicle_id, captured_at_utc, lat, lon, speed_kmh,
                fix_status, source_device, trip_id, local_sequence, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vehicle_id,
                captured_at_utc,
                lat,
                lon,
                speed_kmh,
                fix_status,
                source_device,
                trip_id,
                local_sequence,
                payload_json,
            ),
        )
        await db.commit()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to insert GPS point into %s: %s", DB_PATH, exc)


async def insert_heartbeat(
    *,
    vehicle_id: str,
    heartbeat_type: str,
    captured_at_utc: str,
    payload: dict[str, Any],
) -> None:
    payload_json = json.dumps(payload)

    try:
        db = await _get_db_connection()
        await db.execute(
            """
            INSERT INTO heartbeats (vehicle_id, heartbeat_type, captured_at_utc, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (vehicle_id, heartbeat_type, captured_at_utc, payload_json),
        )
        await db.commit()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to insert heartbeat into %s: %s", DB_PATH, exc)


async def record_edge_health(payload: dict[str, Any]) -> None:
    try:
        db = await _get_db_connection()
        await db.execute(
            """
            INSERT INTO edge_health (
                captured_at_utc,
                last_gps_fix_utc,
                last_upload_success_utc,
                queue_depth,
                disk_free_mb,
                wifi_state,
                process_state,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.get("captured_at_utc", _utc_now_iso()),
                payload.get("last_gps_fix_utc"),
                payload.get("last_upload_success_utc"),
                payload.get("queue_depth", 0),
                payload.get("disk_free_mb"),
                payload.get("wifi_state"),
                payload.get("process_state"),
                json.dumps(payload),
            ),
        )
        await db.commit()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to record edge health: %s", exc)


async def get_pending_gps_points(limit: int = 100) -> list[tuple[int, str, str, int]]:
    """Return pending gps rows: (id, captured_at_utc, payload_json, local_sequence)."""
    try:
        db = await _get_db_connection()
        async with db.execute(
            """
            SELECT id, captured_at_utc, payload_json, local_sequence
            FROM gps_points
            WHERE sent_at_utc IS NULL
            ORDER BY CASE WHEN fix_status = 'locked' THEN 0 ELSE 1 END, captured_at_utc ASC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            return await cursor.fetchall()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to fetch pending gps points: %s", exc)
        return []


async def get_pending_heartbeats(limit: int = 50) -> list[tuple[int, str, str]]:
    """Return pending heartbeat rows: (id, captured_at_utc, payload_json)."""
    try:
        db = await _get_db_connection()
        async with db.execute(
            """
            SELECT id, captured_at_utc, payload_json
            FROM heartbeats
            WHERE sent_at_utc IS NULL
            ORDER BY captured_at_utc ASC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            return await cursor.fetchall()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to fetch pending heartbeats: %s", exc)
        return []


async def mark_gps_points_sent(row_ids: list[int], sent_at_utc: str | None = None) -> None:
    if not row_ids:
        return

    sent_at = sent_at_utc or _utc_now_iso()
    placeholders = ",".join("?" for _ in row_ids)
    params = [sent_at, *row_ids]

    try:
        db = await _get_db_connection()
        await db.execute(
            f"UPDATE gps_points SET sent_at_utc = ? WHERE id IN ({placeholders})",  # nosec B608
            params,
        )
        await db.commit()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to mark GPS rows sent: %s", exc)


async def mark_heartbeats_sent(row_ids: list[int], sent_at_utc: str | None = None) -> None:
    if not row_ids:
        return

    sent_at = sent_at_utc or _utc_now_iso()
    placeholders = ",".join("?" for _ in row_ids)
    params = [sent_at, *row_ids]

    try:
        db = await _get_db_connection()
        await db.execute(
            f"UPDATE heartbeats SET sent_at_utc = ? WHERE id IN ({placeholders})",  # nosec B608
            params,
        )
        await db.commit()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to mark heartbeat rows sent: %s", exc)


async def increment_gps_attempts(row_ids: list[int]) -> None:
    if not row_ids:
        return

    placeholders = ",".join("?" for _ in row_ids)
    try:
        db = await _get_db_connection()
        await db.execute(
            f"UPDATE gps_points SET attempt_count = attempt_count + 1 WHERE id IN ({placeholders})",  # nosec B608
            row_ids,
        )
        await db.commit()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to increment GPS attempts: %s", exc)


async def increment_heartbeat_attempts(row_ids: list[int]) -> None:
    if not row_ids:
        return

    placeholders = ",".join("?" for _ in row_ids)
    try:
        db = await _get_db_connection()
        await db.execute(
            f"UPDATE heartbeats SET attempt_count = attempt_count + 1 WHERE id IN ({placeholders})",  # nosec B608
            row_ids,
        )
        await db.commit()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to increment heartbeat attempts: %s", exc)


async def get_db_stats() -> dict[str, int]:
    try:
        db = await _get_db_connection()
        async with db.execute("SELECT COUNT(*) FROM gps_points WHERE sent_at_utc IS NULL") as cursor:
            pending_gps_row = await cursor.fetchone()
        async with db.execute("SELECT COUNT(*) FROM heartbeats WHERE sent_at_utc IS NULL") as cursor:
            pending_hb_row = await cursor.fetchone()

        async with db.execute("SELECT COUNT(*) FROM gps_points") as cursor:
            gps_total_row = await cursor.fetchone()
        async with db.execute("SELECT COUNT(*) FROM heartbeats") as cursor:
            hb_total_row = await cursor.fetchone()

        pending_gps = pending_gps_row[0] if pending_gps_row else 0
        pending_heartbeats = pending_hb_row[0] if pending_hb_row else 0
        return {
            "pending_gps_points": pending_gps,
            "pending_heartbeats": pending_heartbeats,
            "queue_depth": pending_gps + pending_heartbeats,
            "gps_total": gps_total_row[0] if gps_total_row else 0,
            "heartbeats_total": hb_total_row[0] if hb_total_row else 0,
        }
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to fetch DB stats: %s", exc)
        return {
            "pending_gps_points": -1,
            "pending_heartbeats": -1,
            "queue_depth": -1,
            "gps_total": -1,
            "heartbeats_total": -1,
        }


async def get_latest_power_snapshot(vehicle_id: str) -> dict[str, Any] | None:
    """Return the most recent power snapshot payload for a vehicle, if present."""
    try:
        db = await _get_db_connection()
        async with db.execute(
            """
            SELECT occurred_at, payload
            FROM power_readings
            WHERE vehicle_id = ?
            ORDER BY occurred_at DESC, id DESC
            LIMIT 1
            """,
            (vehicle_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None

        occurred_at, payload_json = row
        return {
            "occurred_at": str(occurred_at),
            "payload": json.loads(str(payload_json)),
        }
    except aiosqlite.OperationalError as exc:
        if "no such table" in str(exc).lower():
            logger.warning("Power snapshot table is unavailable: %s", exc)
            return None
        logger.error("Failed to fetch latest power snapshot: %s", exc)
        return None
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to fetch latest power snapshot: %s", exc)
        return None


async def purge_old_sent_rows(days: int = 7) -> int:
    """Delete rows already sent to cloud and older than `days` days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    deleted_total = 0
    try:
        db = await _get_db_connection()
        gps_cursor = await db.execute(
            "DELETE FROM gps_points WHERE sent_at_utc IS NOT NULL AND captured_at_utc < ?",
            (cutoff,),
        )
        hb_cursor = await db.execute(
            "DELETE FROM heartbeats WHERE sent_at_utc IS NOT NULL AND captured_at_utc < ?",
            (cutoff,),
        )
        await db.commit()
        deleted_total = (gps_cursor.rowcount or 0) + (hb_cursor.rowcount or 0)
        if deleted_total:
            logger.info("Purged %d old sent rows older than %d days", deleted_total, days)
        return deleted_total
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to purge old sent rows: %s", exc)
        return deleted_total
