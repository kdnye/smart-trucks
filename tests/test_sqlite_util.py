"""Tests for shared/sqlite_util.py — busy_timeout/WAL connect + lock retry.

Pure stdlib (sqlite3) + shared.env; runnable under pytest or standalone:
    python3 tests/test_sqlite_util.py
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

try:
    import pytest
except Exception:  # pragma: no cover - pytest not installed in bare env
    pytest = None

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared import sqlite_util  # noqa: E402


def test_connect_sets_busy_timeout_and_wal():
    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite_util.connect(str(Path(tmp) / "t.db"))
        try:
            busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
    assert busy == sqlite_util.SQLITE_BUSY_TIMEOUT_MS
    assert str(journal).lower() == "wal"


def test_is_locked_error():
    assert sqlite_util.is_locked_error(sqlite3.OperationalError("database is locked"))
    assert not sqlite_util.is_locked_error(sqlite3.OperationalError("no such table: x"))


def test_run_with_retry_succeeds_after_transient_lock():
    saved = sqlite_util.SQLITE_LOCKED_RETRY_BASE_DELAY_SECONDS
    sqlite_util.SQLITE_LOCKED_RETRY_BASE_DELAY_SECONDS = 0.0  # no real sleeping
    calls = {"n": 0}

    def _op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "done"

    try:
        result = sqlite_util.run_with_retry(_op, operation_name="t")
    finally:
        sqlite_util.SQLITE_LOCKED_RETRY_BASE_DELAY_SECONDS = saved

    assert result == "done"
    assert calls["n"] == 3  # failed twice, succeeded on the third


def test_run_with_retry_gives_up_after_attempts():
    saved_delay = sqlite_util.SQLITE_LOCKED_RETRY_BASE_DELAY_SECONDS
    saved_count = sqlite_util.SQLITE_LOCKED_RETRY_COUNT
    sqlite_util.SQLITE_LOCKED_RETRY_BASE_DELAY_SECONDS = 0.0
    sqlite_util.SQLITE_LOCKED_RETRY_COUNT = 2  # → 3 attempts total
    calls = {"n": 0}

    def _op():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    try:
        raised = False
        try:
            sqlite_util.run_with_retry(_op, operation_name="t")
        except sqlite3.OperationalError:
            raised = True
    finally:
        sqlite_util.SQLITE_LOCKED_RETRY_BASE_DELAY_SECONDS = saved_delay
        sqlite_util.SQLITE_LOCKED_RETRY_COUNT = saved_count

    assert raised
    assert calls["n"] == 3  # retry_count + 1 attempts, then re-raise


def test_run_with_retry_does_not_retry_non_lock_error():
    calls = {"n": 0}

    def _op():
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: ble_scans")

    raised = False
    try:
        sqlite_util.run_with_retry(_op, operation_name="t")
    except sqlite3.OperationalError:
        raised = True

    assert raised
    assert calls["n"] == 1  # non-lock error is not retried


def _run_standalone() -> int:
    fns = sorted(n for n in dict(globals()) if n.startswith("test_"))
    failures = 0
    for name in fns:
        try:
            globals()[name]()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
