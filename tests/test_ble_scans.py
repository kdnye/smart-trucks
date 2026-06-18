"""Test the ble_scans store-and-forward round-trip in a temp SQLite DB.

Pure sqlite3 — does NOT import bleak or any hardware library. Replicates the
table DDL ble-sensor creates and the exact SELECT sync-service runs, then marks
the row sent and asserts the pending count returns to zero.

Runnable under pytest (``python3 -m pytest tests/test_ble_scans.py``) or
standalone (``python3 tests/test_ble_scans.py``).
"""
import json
import os
import sqlite3
import sys
import tempfile

# Table DDL as created by ble-sensor (_init_shared_queue_store) and mirrored in
# telematics-edge db.init_db().
_CREATE_BLE_SCANS = """
CREATE TABLE IF NOT EXISTS ble_scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at_utc TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    sent_at_utc     TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0
);
"""

# Exact SELECT sync-service uses to load pending rows (_load_pending_rows).
_SELECT_PENDING = """
SELECT id, captured_at_utc, payload_json
FROM ble_scans
WHERE sent_at_utc IS NULL
ORDER BY captured_at_utc ASC, id ASC
LIMIT ?
"""


def _pending_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM ble_scans WHERE sent_at_utc IS NULL"
    ).fetchone()[0]


def test_ble_scan_store_and_forward_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "telematics.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(_CREATE_BLE_SCANS)
            conn.commit()

            # Enqueue a ble_sensor_scan payload (as ble-sensor's _enqueue_ble_scan does).
            payload = {
                "event_type": "ble_sensor_scan",
                "vehicle_id": "TRK-905",
                "pi_id": "smart-truck-pi-01",
                "captured_at_utc": "2026-04-28T17:51:30+00:00",
                "idempotency_key": "TRK-905:ble_sensor_scan:2026-04-28T17:51:30+00:00:abc123def456",
                "sensor_count": 1,
                "sensors": [
                    {"device_id": "A4:C1:38:11:22:33", "mac_address": "A4:C1:38:11:22:33", "rssi": -67}
                ],
            }
            conn.execute(
                "INSERT INTO ble_scans (captured_at_utc, payload_json) VALUES (?, ?)",
                (payload["captured_at_utc"], json.dumps(payload)),
            )
            conn.commit()

            assert _pending_count(conn) == 1

            # Run the exact sync-service pending SELECT.
            rows = conn.execute(_SELECT_PENDING, (50,)).fetchall()
            assert len(rows) == 1
            row_id, captured_at_utc, payload_json = rows[0]
            assert captured_at_utc == payload["captured_at_utc"]
            loaded = json.loads(payload_json)
            assert loaded["event_type"] == "ble_sensor_scan"
            assert loaded["pi_id"] == "smart-truck-pi-01"
            assert loaded["idempotency_key"] == payload["idempotency_key"]

            # Mark sent (as sync-service's _mark_rows_sent does).
            conn.execute(
                "UPDATE ble_scans SET sent_at_utc = CURRENT_TIMESTAMP WHERE id IN (?)",
                (row_id,),
            )
            conn.commit()

            assert _pending_count(conn) == 0
            # No longer returned by the pending SELECT.
            assert conn.execute(_SELECT_PENDING, (50,)).fetchall() == []
        finally:
            conn.close()


def _run_standalone():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # pylint: disable=broad-except
                failures += 1
                print(f"ERROR {name}: {exc}")
    if failures:
        print(f"\n{failures} test(s) failed.")
        sys.exit(1)
    print("\nAll tests passed.")


if __name__ == "__main__":
    _run_standalone()
