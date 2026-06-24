import asyncio
import json
import logging
import os
import sqlite3
from typing import Any

import requests

from shared.env import read_int_env

DB_PATH = os.getenv("DB_PATH", "/data/telematics.db")
SYNC_INTERVAL_SECONDS = read_int_env("SYNC_INTERVAL_SECONDS", 60)
SYNC_BATCH_SIZE = read_int_env("SYNC_BATCH_SIZE", 50, minimum=1)
HTTP_TIMEOUT_SECONDS = read_int_env("HTTP_TIMEOUT_SECONDS", 15)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [sync-service] %(message)s",
)
logger = logging.getLogger(__name__)


def _load_pending_rows(table: str, limit: int) -> list[tuple[int, str, dict[str, Any]]]:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            f"""
            SELECT id, captured_at_utc, payload_json
            FROM {table}
            WHERE sent_at_utc IS NULL
            ORDER BY captured_at_utc ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [(int(r[0]), str(r[1]), json.loads(str(r[2]))) for r in rows]
    finally:
        conn.close()


def _mark_rows_sent(table: str, row_ids: list[int]) -> None:
    if not row_ids:
        return
    placeholders = ",".join("?" for _ in row_ids)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            f"UPDATE {table} SET sent_at_utc = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
            row_ids,
        )
        conn.commit()
    finally:
        conn.close()


def _increment_attempts(table: str, row_ids: list[int]) -> None:
    if not row_ids:
        return
    placeholders = ",".join("?" for _ in row_ids)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            f"UPDATE {table} SET attempt_count = attempt_count + 1 WHERE id IN ({placeholders})",
            row_ids,
        )
        conn.commit()
    finally:
        conn.close()


async def _apply_all(op, ids_by_table: dict[str, list[int]]) -> None:
    """Run a per-table write across all tables SEQUENTIALLY.

    SQLite allows only one writer, so concurrent writes (e.g. via asyncio.gather)
    just contend for the lock with no benefit; serialize them. Empty id lists are
    no-ops in the underlying ops.
    """
    for table_name, row_ids in ids_by_table.items():
        await asyncio.to_thread(op, table_name, row_ids)


def _send_events(events: list[dict[str, Any]]) -> requests.Response:
    webhook_url = os.environ.get("WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("WEBHOOK_URL is not set.")

    headers = {}
    api_key = os.environ.get("EDGE_INGEST_KEY")
    if api_key:
        headers["X-API-Key"] = api_key

    return requests.post(
        webhook_url,
        json={"events": events},
        headers=headers or None,
        timeout=HTTP_TIMEOUT_SECONDS,
    )


async def sync_cycle() -> None:
    pending = {
        "heartbeats": await asyncio.to_thread(_load_pending_rows, "heartbeats", SYNC_BATCH_SIZE),
        "gps_points": await asyncio.to_thread(_load_pending_rows, "gps_points", SYNC_BATCH_SIZE),
        "edge_health": await asyncio.to_thread(_load_pending_rows, "edge_health", SYNC_BATCH_SIZE),
        "ble_scans": await asyncio.to_thread(_load_pending_rows, "ble_scans", SYNC_BATCH_SIZE),
    }
    all_rows: list[tuple[str, int, str, dict[str, Any]]] = []
    for table_name, rows in pending.items():
        for row_id, captured_at_utc, payload in rows:
            all_rows.append((table_name, row_id, captured_at_utc, payload))

    all_rows.sort(key=lambda item: (item[2], item[1]))
    if not all_rows:
        logger.debug("No pending rows found; skipping cycle.")
        return

    events = [row[3] for row in all_rows]

    ids_by_table: dict[str, list[int]] = {"heartbeats": [], "gps_points": [], "edge_health": [], "ble_scans": []}
    for table_name, row_id, _, _ in all_rows:
        ids_by_table[table_name].append(row_id)

    try:
        response = await asyncio.to_thread(_send_events, events)
    except (requests.RequestException, OSError) as exc:
        # Network/transport failure (DNS, connection refused, timeout, etc.).
        # Leave rows pending (sent_at_utc NULL) so they retry on the next cycle,
        # but bump attempt_count for backoff/observability visibility.
        await _apply_all(_increment_attempts, ids_by_table)
        logger.warning("Network error sending events: %s. Rows remain pending for retry.", exc)
        return

    if 200 <= response.status_code < 300:
        await _apply_all(_mark_rows_sent, ids_by_table)
        logger.info(
            "Synced events batch (heartbeats=%d gps_points=%d edge_health=%d ble_scans=%d).",
            len(ids_by_table["heartbeats"]),
            len(ids_by_table["gps_points"]),
            len(ids_by_table["edge_health"]),
            len(ids_by_table["ble_scans"]),
        )
    elif 400 <= response.status_code < 500:
        await _apply_all(_increment_attempts, ids_by_table)
        if response.status_code in (401, 403):
            logger.error(
                "SETUP: %s from the ingest endpoint — the API key this device sends does "
                "NOT match the cloud ingest secret. Action: set EDGE_INGEST_KEY for this "
                "device in Balena to the same value the ingest function expects. "
                "WEBHOOK_URL=%s",
                response.status_code,
                os.environ.get("WEBHOOK_URL", "<unset>"),
            )
        else:
            logger.warning("4xx response (%s): incremented attempts and dropping retry.", response.status_code)
    else:
        logger.warning("5xx/non-2xx response (%s): leaving rows pending for retry.", response.status_code)


def _validate_startup_config() -> None:
    """Log obvious, actionable setup problems once at boot."""
    if not os.environ.get("WEBHOOK_URL"):
        logger.error(
            "SETUP: WEBHOOK_URL is not set — nothing can be uploaded. "
            "Action: set WEBHOOK_URL (the cloud ingest URL) for this device in Balena."
        )
    if not os.environ.get("EDGE_INGEST_KEY"):
        logger.error(
            "SETUP: EDGE_INGEST_KEY is not set — the cloud will reject every upload with "
            "401. Action: set EDGE_INGEST_KEY in Balena to the cloud ingest secret."
        )
    if (os.environ.get("VEHICLE_ID") or "").strip() in ("", "UNKNOWN_TRUCK"):
        logger.warning(
            "SETUP: VEHICLE_ID is not set — uploaded data will not map to a truck/warehouse. "
            "Action: set VEHICLE_ID for this device in Balena."
        )


async def run() -> None:
    _validate_startup_config()
    logger.info(
        "Starting sync-service loop: interval=%ss db=%s batch_size=%s webhook_url=%s key=%s",
        SYNC_INTERVAL_SECONDS,
        DB_PATH,
        SYNC_BATCH_SIZE,
        "set" if os.environ.get("WEBHOOK_URL") else "UNSET",
        "set" if os.environ.get("EDGE_INGEST_KEY") else "UNSET",
    )
    while True:
        try:
            await sync_cycle()
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Sync cycle failed: %s", exc)
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run())
