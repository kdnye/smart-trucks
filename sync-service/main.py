import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

import requests

DB_PATH = "/data/telematics.db"
SYNC_INTERVAL_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 15

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [sync-service] %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class PendingRows:
    gps_id: int | None
    gps_payload: dict[str, Any]
    power_id: int | None
    power_payload: dict[str, Any]
    ble_ids: list[int]
    ble_payloads: list[dict[str, Any]]

    @property
    def has_data(self) -> bool:
        return self.gps_id is not None or self.power_id is not None or bool(self.ble_ids)


def _row_to_payload(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    payload.pop("synced", None)
    return payload


def _fetch_pending_rows() -> PendingRows:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        gps_row = connection.execute(
            """
            SELECT *
            FROM local_gps
            WHERE synced = 0
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        power_row = connection.execute(
            """
            SELECT *
            FROM local_power
            WHERE synced = 0
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        ble_rows = connection.execute(
            """
            SELECT *
            FROM local_ble
            WHERE synced = 0
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        connection.close()

    return PendingRows(
        gps_id=None if gps_row is None else int(gps_row["id"]),
        gps_payload={} if gps_row is None else _row_to_payload(gps_row),
        power_id=None if power_row is None else int(power_row["id"]),
        power_payload={} if power_row is None else _row_to_payload(power_row),
        ble_ids=[int(row["id"]) for row in ble_rows],
        ble_payloads=[_row_to_payload(row) for row in ble_rows],
    )


def _mark_rows_synced(rows: PendingRows) -> None:
    connection = sqlite3.connect(DB_PATH)
    try:
        connection.execute("BEGIN")

        if rows.gps_id is not None:
            connection.execute("UPDATE local_gps SET synced = 1 WHERE id = ?", (rows.gps_id,))

        if rows.power_id is not None:
            connection.execute("UPDATE local_power SET synced = 1 WHERE id = ?", (rows.power_id,))

        if rows.ble_ids:
            placeholders = ",".join("?" for _ in rows.ble_ids)
            connection.execute(
                f"UPDATE local_ble SET synced = 1 WHERE id IN ({placeholders})",
                rows.ble_ids,
            )

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _build_payload(rows: PendingRows) -> dict[str, Any]:
    return {
        "vehicle_id": os.environ.get("VEHICLE_ID"),
        "event_type": "unified_heartbeat",
        "gps": rows.gps_payload,
        "power": rows.power_payload,
        "ble_scans": rows.ble_payloads,
    }


def _send_payload(payload: dict[str, Any]) -> requests.Response:
    webhook_url = os.environ.get("WEBHOOK_URL")
    if not webhook_url:
        raise RuntimeError("WEBHOOK_URL is not set.")

    return requests.post(
        webhook_url,
        json=payload,
        timeout=HTTP_TIMEOUT_SECONDS,
    )


async def sync_cycle() -> None:
    pending_rows = await asyncio.to_thread(_fetch_pending_rows)
    if not pending_rows.has_data:
        logger.debug("No unsynced local rows found; skipping cycle.")
        return

    payload = _build_payload(pending_rows)
    response = await asyncio.to_thread(_send_payload, payload)

    if response.status_code == 200:
        await asyncio.to_thread(_mark_rows_synced, pending_rows)
        logger.info(
            "Unified heartbeat synced (gps_id=%s power_id=%s ble_count=%s).",
            pending_rows.gps_id,
            pending_rows.power_id,
            len(pending_rows.ble_ids),
        )
        return

    logger.warning(
        "Unified heartbeat not acknowledged (status=%s); leaving rows unsynced.",
        response.status_code,
    )


async def run() -> None:
    logger.info("Starting sync-service loop: interval=%ss db=%s", SYNC_INTERVAL_SECONDS, DB_PATH)
    while True:
        try:
            await sync_cycle()
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Sync cycle failed: %s", exc)
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run())
