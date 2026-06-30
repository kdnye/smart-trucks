"""Tests for ``telematics-edge/db.py`` ``purge_old_sent_rows``.

Loads the edge ``db`` module against a temp SQLite DB and exercises:

* correctness — only rows that are *both* already sent and older than the
  cutoff are deleted (recent-sent and old-unsent rows survive), across every
  store-and-forward queue;
* isolation — the purge must run on its own connection, decoupled from the
  shared autocommit connection's transaction state. The shared connection is
  also used by ``pop_beacon_wake_signal`` (which issues its own ``commit()``)
  from ``parked_scan_worker``, concurrently with the ``maintenance_worker``
  that owns the purge; wrapping the multi-DELETE ``BEGIN IMMEDIATE``/``COMMIT``
  on that shared connection let an interleaved commit close the purge
  transaction mid-flight. This regression test holds an open read transaction
  on the shared connection and asserts the purge still completes — it fails on
  the old shared-connection implementation (nested ``BEGIN`` on the same
  connection) and passes once the purge owns a dedicated connection.

Runnable under pytest (``python3 -m pytest tests/test_purge_old_rows.py``) or
standalone (``python3 tests/test_purge_old_rows.py``).
"""
import asyncio
import importlib.util
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODULE_PATH = REPO_ROOT / "telematics-edge" / "db.py"
SPEC = importlib.util.spec_from_file_location("edge_db", MODULE_PATH)
edge_db = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(edge_db)


_NOW = datetime.now(timezone.utc)
_OLD = (_NOW - timedelta(days=30)).isoformat()
_RECENT = _NOW.isoformat()


def _seed(db_path: str) -> None:
    """Insert, per queue, one old-sent (purgeable), one recent-sent, one old-unsent row."""
    conn = sqlite3.connect(db_path)
    try:
        payload = json.dumps({"k": "v"})
        # (captured_at_utc, sent_at_utc) — first row is the only purge target.
        rows = [(_OLD, _RECENT), (_RECENT, _RECENT), (_OLD, None)]
        for captured, sent in rows:
            conn.execute(
                "INSERT INTO gps_points "
                "(vehicle_id, captured_at_utc, fix_status, local_sequence, payload_json, sent_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("TRK-1", captured, "locked", 1, payload, sent),
            )
            conn.execute(
                "INSERT INTO heartbeats "
                "(vehicle_id, heartbeat_type, captured_at_utc, payload_json, sent_at_utc) "
                "VALUES (?, ?, ?, ?, ?)",
                ("TRK-1", "edge_health", captured, payload, sent),
            )
            conn.execute(
                "INSERT INTO ble_scans (captured_at_utc, payload_json, sent_at_utc) VALUES (?, ?, ?)",
                (captured, payload, sent),
            )
        conn.commit()
    finally:
        conn.close()


def _remaining(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608
    finally:
        conn.close()


async def _reset_and_init(db_path: str) -> None:
    edge_db.DB_PATH = db_path
    edge_db._db_connection = None
    await edge_db.init_db()


async def _close_shared() -> None:
    if edge_db._db_connection is not None:
        await edge_db._db_connection.close()
        edge_db._db_connection = None


async def _run_purge_clean(db_path: str) -> int:
    try:
        await _reset_and_init(db_path)
        _seed(db_path)
        return await edge_db.purge_old_sent_rows(days=7)
    finally:
        await _close_shared()


async def _run_purge_with_open_shared_txn(db_path: str) -> int:
    """Purge while the shared connection holds an open read transaction.

    A read transaction (not BEGIN IMMEDIATE) so WAL still lets the purge's
    dedicated writer commit — the only thing that should break here is a purge
    that (wrongly) reuses the shared connection and trips a nested BEGIN.
    """
    try:
        await _reset_and_init(db_path)
        _seed(db_path)
        shared = await edge_db._get_db_connection()
        await shared.execute("BEGIN;")
        await shared.execute("SELECT COUNT(*) FROM gps_points;")  # materialize the read txn
        try:
            return await edge_db.purge_old_sent_rows(days=7)
        finally:
            if shared.in_transaction:
                await shared.execute("ROLLBACK;")
    finally:
        await _close_shared()


def test_purge_deletes_only_old_sent_rows():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "telematics.db")
        deleted = asyncio.run(_run_purge_clean(db_path))
        # Exactly one purgeable row per queue: gps + heartbeat + ble = 3.
        assert deleted == 3, deleted
        # Recent-sent and old-unsent rows survive in each queue.
        assert _remaining(db_path, "gps_points") == 2
        assert _remaining(db_path, "heartbeats") == 2
        assert _remaining(db_path, "ble_scans") == 2


def test_purge_is_isolated_from_shared_connection_transaction():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "telematics.db")
        deleted = asyncio.run(_run_purge_with_open_shared_txn(db_path))
        # On the dedicated-connection implementation the purge is unaffected by
        # the shared connection's open transaction and still removes all three.
        assert deleted == 3, deleted
        assert _remaining(db_path, "gps_points") == 2
        assert _remaining(db_path, "heartbeats") == 2
        assert _remaining(db_path, "ble_scans") == 2


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
