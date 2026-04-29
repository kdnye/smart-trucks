import asyncio
import json
import logging
import os
import sqlite3
from typing import Any

import requests

DB_PATH = os.getenv("DB_PATH", "/data/telematics.db")
SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "60"))
SYNC_BATCH_SIZE = max(1, int(os.getenv("SYNC_BATCH_SIZE", "50")))
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))

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


def _send_events(events: list[dict[str, Any]]) -> requests.Response:
    webhook_url = os.environ.get("WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("WEBHOOK_URL is not set.")

    headers = {}
    motive_api_key = os.environ.get("MOTIVE_API_KEY")
    if motive_api_key:
        headers["X-API-Key"] = motive_api_key

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
    response = await asyncio.to_thread(_send_events, events)

    ids_by_table: dict[str, list[int]] = {"heartbeats": [], "gps_points": [], "edge_health": []}
    for table_name, row_id, _, _ in all_rows:
        ids_by_table[table_name].append(row_id)

    if 200 <= response.status_code < 300:
        await asyncio.gather(
            asyncio.to_thread(_mark_rows_sent, "heartbeats", ids_by_table["heartbeats"]),
            asyncio.to_thread(_mark_rows_sent, "gps_points", ids_by_table["gps_points"]),
            asyncio.to_thread(_mark_rows_sent, "edge_health", ids_by_table["edge_health"]),
        )
        logger.info(
            "Synced events batch (heartbeats=%d gps_points=%d edge_health=%d).",
            len(ids_by_table["heartbeats"]),
            len(ids_by_table["gps_points"]),
            len(ids_by_table["edge_health"]),
        )
    elif 400 <= response.status_code < 500:
        await asyncio.gather(
            asyncio.to_thread(_increment_attempts, "heartbeats", ids_by_table["heartbeats"]),
            asyncio.to_thread(_increment_attempts, "gps_points", ids_by_table["gps_points"]),
            asyncio.to_thread(_increment_attempts, "edge_health", ids_by_table["edge_health"]),
        )
        logger.warning("4xx response (%s): incremented attempts and dropping retry.", response.status_code)
    else:
        logger.warning("5xx/non-2xx response (%s): leaving rows pending for retry.", response.status_code)


async def run() -> None:
    logger.info(
        "Starting sync-service loop: interval=%ss db=%s batch_size=%s",
        SYNC_INTERVAL_SECONDS,
        DB_PATH,
        SYNC_BATCH_SIZE,
    )
    while True:
        try:
            await sync_cycle()
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Sync cycle failed: %s", exc)
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run())
