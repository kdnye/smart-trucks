"""Tests for sync-service aggressive draining + priority beacon.

Loads ``sync-service/main.py`` against a temp SQLite DB and a stubbed HTTP
sender, then exercises one drain cycle. No real network. Runnable under pytest
(``python3 -m pytest tests/test_sync_drain.py``) or standalone
(``python3 tests/test_sync_drain.py``).
"""
import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODULE_PATH = REPO_ROOT / "sync-service" / "main.py"
SPEC = importlib.util.spec_from_file_location("sync_service_main", MODULE_PATH)
sync_main = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(sync_main)

_TABLES = ("heartbeats", "gps_points", "edge_health", "ble_scans")
_CREATE = """
CREATE TABLE IF NOT EXISTS {table} (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at_utc TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    sent_at_utc     TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0
);
"""


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _make_db(tmp: str) -> str:
    db_path = os.path.join(tmp, "telematics.db")
    conn = sqlite3.connect(db_path)
    try:
        for table in _TABLES:
            conn.execute(_CREATE.format(table=table))
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert(db_path: str, table: str, payloads: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for p in payloads:
            conn.execute(
                f"INSERT INTO {table} (captured_at_utc, payload_json) VALUES (?, ?)",
                (p["captured_at_utc"], json.dumps(p)),
            )
        conn.commit()
    finally:
        conn.close()


def _pending(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE sent_at_utc IS NULL").fetchone()[0]
    finally:
        conn.close()


def _attempts(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COALESCE(MAX(attempt_count), 0) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _configure(db_path: str, batch_size: int = 3) -> None:
    sync_main.DB_PATH = db_path
    sync_main.SYNC_BATCH_SIZE = batch_size
    os.environ["WEBHOOK_URL"] = "https://example.invalid/ingest"


def _gps_payloads(n: int) -> list[dict]:
    return [
        {
            "event_type": "edge_telematics_heartbeat",
            "source": "gps_track",
            "vehicle_id": "TRK-905",
            "captured_at_utc": f"2026-04-28T17:5{i // 10}:{i % 10}0+00:00",
        }
        for i in range(n)
    ]


def test_full_batch_marks_sent_and_reports_more_pending():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _make_db(tmp)
        _configure(db_path, batch_size=3)
        _insert(db_path, "gps_points", _gps_payloads(5))

        sent_batches = []

        def fake_send(events):
            sent_batches.append(events)
            return _FakeResponse(200)

        sync_main._send_events = fake_send

        outcome = asyncio.run(sync_main.sync_cycle())

        assert outcome.ok is True
        assert outcome.more_pending is True  # 3 loaded == batch -> backlog remains
        assert outcome.sent == 3
        assert _pending(db_path, "gps_points") == 2  # 3 of 5 marked sent
        assert len(sent_batches[0]) == 3


def test_network_error_keeps_rows_pending_and_bumps_attempts():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _make_db(tmp)
        _configure(db_path, batch_size=3)
        _insert(db_path, "gps_points", _gps_payloads(2))

        def fake_send(events):
            raise requests.ConnectionError("no route to host")

        sync_main._send_events = fake_send

        outcome = asyncio.run(sync_main.sync_cycle())

        assert outcome.ok is False
        assert outcome.sent == 0
        assert _pending(db_path, "gps_points") == 2  # nothing lost
        assert _attempts(db_path, "gps_points") == 1  # bumped for backoff visibility


def test_small_queue_drains_without_more_pending():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _make_db(tmp)
        _configure(db_path, batch_size=3)
        _insert(db_path, "heartbeats", _gps_payloads(1))

        sync_main._send_events = lambda events: _FakeResponse(200)

        outcome = asyncio.run(sync_main.sync_cycle())

        assert outcome.ok is True
        assert outcome.more_pending is False
        assert _pending(db_path, "heartbeats") == 0


def test_priority_beacon_sends_newest_heartbeat_without_marking_sent():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _make_db(tmp)
        _configure(db_path, batch_size=3)
        # Three heartbeats with increasing timestamps; newest is the 17:53 one.
        _insert(
            db_path,
            "heartbeats",
            [
                {"event_type": "edge_telematics_heartbeat", "captured_at_utc": "2026-04-28T17:51:00+00:00", "n": 1},
                {"event_type": "edge_telematics_heartbeat", "captured_at_utc": "2026-04-28T17:52:00+00:00", "n": 2},
                {"event_type": "edge_telematics_heartbeat", "captured_at_utc": "2026-04-28T17:53:00+00:00", "n": 3},
            ],
        )

        captured = []

        def fake_send(events):
            captured.append(events)
            return _FakeResponse(200)

        sync_main._send_events = fake_send

        asyncio.run(sync_main._send_priority_beacon())

        assert len(captured) == 1
        beacon = captured[0]
        # Newest heartbeat first; rows remain queued (not marked sent).
        assert beacon[0]["n"] == 3
        assert _pending(db_path, "heartbeats") == 3


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
