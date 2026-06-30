"""Shared SQLite helpers for the synchronous edge writers (sync-service, ble-sensor).

Several containers write the shared ``/data/telematics.db`` (WAL). SQLite allows
only one writer at a time, so under contention a writer can hit
``sqlite3.OperationalError: database is locked``. Without a high ``busy_timeout``
(the Python ``sqlite3`` default is only 5s) and a retry, that surfaces as an
aborted operation — a whole ``sync_cycle`` rolled back and re-loaded, or a
dropped BLE scan. These helpers give the plain-``sqlite3`` callers the same
resilience ``telematics-edge/db.py`` already has on its aiosqlite path.

Import-light on purpose (stdlib + ``shared.env``) so it's unit-testable without
the hardware/runtime deps. ``run_with_retry`` uses ``time.sleep`` and so must be
called from a worker thread (e.g. ``asyncio.to_thread``), not an event loop.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Callable, TypeVar

from shared.env import read_float_env, read_int_env

logger = logging.getLogger(__name__)

SQLITE_CONNECT_TIMEOUT_SECONDS = read_float_env("SQLITE_CONNECT_TIMEOUT_SECONDS", 30.0)
SQLITE_BUSY_TIMEOUT_MS = read_int_env("SQLITE_BUSY_TIMEOUT_MS", 30000)
SQLITE_LOCKED_RETRY_COUNT = read_int_env("SQLITE_LOCKED_RETRY_COUNT", 5)
SQLITE_LOCKED_RETRY_BASE_DELAY_SECONDS = read_float_env(
    "SQLITE_LOCKED_RETRY_BASE_DELAY_SECONDS", 0.05
)

_T = TypeVar("_T")


def connect(db_path: str, *, isolation_level: str | None = "DEFERRED") -> sqlite3.Connection:
    """Open a SQLite connection configured for shared-writer contention.

    Applies ``busy_timeout`` (wait for the lock instead of erroring at the 5s
    Python default), WAL, and ``synchronous=NORMAL`` on every connection. PRAGMA
    statements are not DML, so the Python ``sqlite3`` module does not open a
    transaction for them — safe even with ``isolation_level="IMMEDIATE"``.
    """
    conn = sqlite3.connect(
        db_path,
        timeout=SQLITE_CONNECT_TIMEOUT_SECONDS,
        isolation_level=isolation_level,
    )
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def is_locked_error(exc: BaseException) -> bool:
    return "database is locked" in str(exc).lower()


def run_with_retry(operation: Callable[[], _T], *, operation_name: str) -> _T:
    """Run a blocking SQLite operation, retrying only on 'database is locked'.

    A safety net beyond ``busy_timeout`` for the rare case a lock is held longer
    than the timeout. The operation must be a single atomic transaction so a
    retry can safely re-run it (no partial commit). Synchronous — call from a
    worker thread, not directly on an event loop.
    """
    attempts = max(1, SQLITE_LOCKED_RETRY_COUNT + 1)
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if not is_locked_error(exc) or attempt == attempts:
                raise
            delay_seconds = SQLITE_LOCKED_RETRY_BASE_DELAY_SECONDS * attempt
            logger.warning(
                "%s hit SQLite lock contention (attempt %d/%d); retrying in %.3fs",
                operation_name,
                attempt,
                attempts,
                delay_seconds,
            )
            time.sleep(delay_seconds)
    assert last_exc is not None  # unreachable: loop returns or raises
    raise last_exc
