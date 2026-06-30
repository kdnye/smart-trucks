import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import requests

from shared import sentry_flag, sqlite_util
from shared.env import read_bool_env, read_float_env, read_int_env

DB_PATH = os.getenv("DB_PATH", "/data/telematics.db")
SYNC_INTERVAL_SECONDS = read_int_env("SYNC_INTERVAL_SECONDS", 60)
SYNC_BATCH_SIZE = read_int_env("SYNC_BATCH_SIZE", 100, minimum=1)
# BLE scan payloads are far larger (tens of sensors each) than heartbeats/gps, so
# drain them in smaller chunks to bound peak memory while clearing a backlog on
# the 512 MB Pi (the likely overnight OOM trigger).
SYNC_BLE_BATCH_SIZE = read_int_env("SYNC_BLE_BATCH_SIZE", 50, minimum=1)
# Delay between back-to-back drain batches while a backlog is clearing. Small so
# a multi-hour offline backlog drains in minutes, not hours, but non-zero so we
# don't busy-spin the CPU or hammer the ingest endpoint.
SYNC_DRAIN_DELAY_SECONDS = read_float_env("SYNC_DRAIN_DELAY_SECONDS", 0.5, minimum=0.0)
HTTP_TIMEOUT_SECONDS = read_int_env("HTTP_TIMEOUT_SECONDS", 30)
# On reconnect after an outage, push the newest position first so the dashboard
# shows the truck "online and here" immediately, before the historical backfill
# streams in chronologically behind it.
PRIORITY_BEACON_ENABLED = read_bool_env("SYNC_PRIORITY_BEACON_ENABLED", True)

# Tables drained, in the order their rows are loaded. The combined batch is
# re-sorted by captured_at so the cloud ingests chronologically regardless.
SYNC_TABLES = ("heartbeats", "gps_points", "edge_health", "ble_scans")


def _batch_limit(table: str) -> int:
    """Per-table drain size: smaller for the large ble_scans payloads."""
    return SYNC_BLE_BATCH_SIZE if table == "ble_scans" else SYNC_BATCH_SIZE

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [sync-service] %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncOutcome:
    """Result of one drain cycle, used to decide whether to keep draining."""

    ok: bool          # send succeeded (or nothing to send)
    sent: int         # rows marked sent this cycle
    more_pending: bool  # a table was saturated, so more rows likely remain


def _load_pending_rows(table: str, limit: int) -> list[tuple[int, str, dict[str, Any]]]:
    conn = sqlite_util.connect(DB_PATH)
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


def _load_latest_payloads(table: str, limit: int = 1) -> list[dict[str, Any]]:
    """Return the newest unsent payloads for a table (for the priority beacon)."""
    conn = sqlite_util.connect(DB_PATH)
    try:
        rows = conn.execute(
            f"""
            SELECT payload_json
            FROM {table}
            WHERE sent_at_utc IS NULL
            ORDER BY captured_at_utc DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [json.loads(str(r[0])) for r in rows]
    finally:
        conn.close()


def _mark_rows_sent(table: str, row_ids: list[int]) -> None:
    if not row_ids:
        return
    placeholders = ",".join("?" for _ in row_ids)

    def _op() -> None:
        conn = sqlite_util.connect(DB_PATH)
        try:
            conn.execute(
                f"UPDATE {table} SET sent_at_utc = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
                row_ids,
            )
            conn.commit()
        finally:
            conn.close()

    # Retry on lock so a transient writer collision doesn't abort the whole
    # sync_cycle and re-upload the batch (this is the exact failure that wedged
    # the pipeline). Single UPDATE in one transaction → safe to re-run.
    sqlite_util.run_with_retry(_op, operation_name=f"mark_rows_sent[{table}]")


def _increment_attempts(table: str, row_ids: list[int]) -> None:
    if not row_ids:
        return
    placeholders = ",".join("?" for _ in row_ids)

    def _op() -> None:
        conn = sqlite_util.connect(DB_PATH)
        try:
            conn.execute(
                f"UPDATE {table} SET attempt_count = attempt_count + 1 WHERE id IN ({placeholders})",
                row_ids,
            )
            conn.commit()
        finally:
            conn.close()

    sqlite_util.run_with_retry(_op, operation_name=f"increment_attempts[{table}]")


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


async def _send_priority_beacon() -> None:
    """Best-effort: ship the newest position/health so the map updates instantly.

    The rows are NOT marked sent — they stay queued and are delivered again in
    chronological order during the normal drain. The cloud ingest dedups
    heartbeats on (pi_id, captured_at_utc), so the replay is harmless.
    """
    try:
        beacon = await asyncio.to_thread(_load_latest_payloads, "heartbeats", 1)
        beacon += await asyncio.to_thread(_load_latest_payloads, "edge_health", 1)
        if not beacon:
            return
        response = await asyncio.to_thread(_send_events, beacon)
        if 200 <= response.status_code < 300:
            logger.info("Priority beacon delivered current state ahead of %d-row backfill.", len(beacon))
        else:
            logger.warning("Priority beacon got HTTP %s; backfill will catch up.", response.status_code)
    except Exception as exc:  # pylint: disable=broad-except
        # Best-effort only: a DB lock, malformed payload, or transport error here
        # must never crash the sync loop (it's called outside run()'s try/except).
        logger.warning("Priority beacon send failed (%s); backfill will catch up.", exc)


async def sync_cycle() -> SyncOutcome:
    pending = {
        table: await asyncio.to_thread(_load_pending_rows, table, _batch_limit(table))
        for table in SYNC_TABLES
    }
    # A saturated table almost certainly has more rows waiting — signal the
    # caller to drain again immediately instead of sleeping the full interval.
    more_pending = any(len(rows) >= _batch_limit(table) for table, rows in pending.items())

    all_rows: list[tuple[str, int, str, dict[str, Any]]] = []
    for table_name, rows in pending.items():
        for row_id, captured_at_utc, payload in rows:
            all_rows.append((table_name, row_id, captured_at_utc, payload))

    all_rows.sort(key=lambda item: (item[2], item[1]))
    if not all_rows:
        logger.debug("No pending rows found; skipping cycle.")
        return SyncOutcome(ok=True, sent=0, more_pending=False)

    events = [row[3] for row in all_rows]

    ids_by_table: dict[str, list[int]] = {table: [] for table in SYNC_TABLES}
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
        return SyncOutcome(ok=False, sent=0, more_pending=more_pending)

    if 200 <= response.status_code < 300:
        await _apply_all(_mark_rows_sent, ids_by_table)
        logger.info(
            "Synced events batch (heartbeats=%d gps_points=%d edge_health=%d ble_scans=%d).",
            len(ids_by_table["heartbeats"]),
            len(ids_by_table["gps_points"]),
            len(ids_by_table["edge_health"]),
            len(ids_by_table["ble_scans"]),
        )
        return SyncOutcome(ok=True, sent=len(events), more_pending=more_pending)
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
        return SyncOutcome(ok=False, sent=0, more_pending=more_pending)
    else:
        logger.warning("5xx/non-2xx response (%s): leaving rows pending for retry.", response.status_code)
        return SyncOutcome(ok=False, sent=0, more_pending=more_pending)


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
        "Starting sync-service loop: interval=%ss drain_delay=%ss db=%s batch_size=%s "
        "ble_batch_size=%s priority_beacon=%s webhook_url=%s key=%s",
        SYNC_INTERVAL_SECONDS,
        SYNC_DRAIN_DELAY_SECONDS,
        DB_PATH,
        SYNC_BATCH_SIZE,
        SYNC_BLE_BATCH_SIZE,
        PRIORITY_BEACON_ENABLED,
        "set" if os.environ.get("WEBHOOK_URL") else "UNSET",
        "set" if os.environ.get("EDGE_INGEST_KEY") else "UNSET",
    )
    # Latch so the "online and here" beacon fires once per backlog, not every
    # batch while a long backfill drains.
    beacon_sent = False
    suspend_flag_path = sentry_flag.flag_path()
    while True:
        # Sentry Mode sleep (set by telematics-edge): stop uploading so the WiFi
        # radio can idle. The flag never appears unless Sentry Mode is enabled.
        if sentry_flag.is_suspended(suspend_flag_path):
            beacon_sent = False
            await asyncio.sleep(SYNC_INTERVAL_SECONDS)
            continue
        try:
            outcome = await sync_cycle()
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Sync cycle failed: %s", exc)
            beacon_sent = False
            await asyncio.sleep(SYNC_INTERVAL_SECONDS)
            continue

        if outcome.ok and outcome.more_pending:
            # Backlog is clearing: announce the current position once, then keep
            # draining back-to-back instead of sleeping the full interval.
            if PRIORITY_BEACON_ENABLED and not beacon_sent:
                await _send_priority_beacon()
                beacon_sent = True
            await asyncio.sleep(SYNC_DRAIN_DELAY_SECONDS)
            continue

        # Queue drained, or an error/backoff cycle — idle until the next tick.
        beacon_sent = False
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run())
