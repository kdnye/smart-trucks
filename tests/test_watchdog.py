"""Tests for the wifi-provisioner edge watchdog (wifi-provisioner/watchdog.py).

Import-light module (no aiohttp), so it loads and runs in CI and locally.
Covers the pure restart decision, ISO parsing, and the heartbeat-age reader
against a temp SQLite DB. Loaded via importlib with REPO_ROOT on sys.path so
`shared.*` resolves.
"""
import importlib.util
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODULE_PATH = REPO_ROOT / "wifi-provisioner" / "edge_watchdog.py"
SPEC = importlib.util.spec_from_file_location("edge_watchdog", MODULE_PATH)
wd = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(wd)


def test_should_restart_thresholds():
    # None (no data / unreadable) never restarts.
    assert wd.watchdog_should_restart(None, stale_seconds=600) is False
    # Below threshold: alive.
    assert wd.watchdog_should_restart(120.0, stale_seconds=600) is False
    # At/above threshold: stale.
    assert wd.watchdog_should_restart(600.0, stale_seconds=600) is True
    assert wd.watchdog_should_restart(900.0, stale_seconds=600) is True


def test_parse_iso_utc():
    aware = wd.parse_iso_utc("2026-06-30T12:00:00+00:00")
    assert aware == datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    # tz-naive is assumed UTC.
    naive = wd.parse_iso_utc("2026-06-30T12:00:00")
    assert naive == datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
    assert wd.parse_iso_utc("not-a-timestamp") is None


def _make_heartbeats_db(path: str, captured_at_utc: str | None) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE heartbeats (captured_at_utc TEXT)")
        if captured_at_utc is not None:
            conn.execute(
                "INSERT INTO heartbeats (captured_at_utc) VALUES (?)", (captured_at_utc,)
            )
        conn.commit()
    finally:
        conn.close()


def test_newest_heartbeat_age_seconds_with_row():
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "telematics.db")
        _make_heartbeats_db(db, "2026-06-30T12:00:00+00:00")
        now = datetime(2026, 6, 30, 12, 5, 0, tzinfo=timezone.utc)  # +300s
        age = wd.newest_heartbeat_age_seconds(db, now=now)
        assert age is not None
        assert abs(age - 300.0) < 1.0


def test_newest_heartbeat_age_seconds_empty_table_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "telematics.db")
        _make_heartbeats_db(db, None)  # table exists, no rows
        assert wd.newest_heartbeat_age_seconds(db) is None


def test_newest_heartbeat_age_seconds_no_table_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "telematics.db")
        # Fresh DB created on connect, but no heartbeats table yet.
        sqlite3.connect(db).close()
        assert wd.newest_heartbeat_age_seconds(db) is None


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
