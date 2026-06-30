"""Cross-service watchdog: restart the edge container when it goes silent.

The in-container edge supervisor (edge/start.sh) restarts individual crashed
co-processes; this catches the harder case where the whole edge process is hung
— it stops producing heartbeats but never exits — by restarting the `edge`
service via the Balena Supervisor API.

Import-light on purpose (stdlib + ``shared`` only; ``aiohttp`` is imported
lazily inside ``BalenaSupervisor``), so the decision logic is unit-testable
without the aiohttp portal import chain in ``main.py``. ``config``/``state`` are
duck-typed (the ``Config``/``State`` dataclasses from ``main.py``).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone

from shared import sqlite_util
from shared.balena_supervisor import BalenaSupervisor

logger = logging.getLogger("wifi-provisioner.watchdog")


def watchdog_should_restart(heartbeat_age_seconds: float | None, *, stale_seconds: float) -> bool:
    """True when the edge's newest heartbeat is older than the stale threshold.

    ``None`` (no heartbeat row yet, DB not ready, or an unparseable timestamp)
    returns False — we only restart on demonstrably stale data, never on missing
    data, so a fresh boot or a transient read error can't trigger a restart.
    """
    if heartbeat_age_seconds is None:
        return False
    return heartbeat_age_seconds >= stale_seconds


def parse_iso_utc(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def newest_heartbeat_age_seconds(db_path: str, *, now: datetime | None = None) -> float | None:
    """Age in seconds of the newest ``heartbeats.captured_at_utc`` in the shared
    DB, or None if the DB/table/rows aren't present yet or the timestamp can't be
    parsed. The edge writes heartbeats locally even while offline, so a stale
    value means the edge process is wedged, not merely disconnected."""
    try:
        conn = sqlite_util.connect(db_path)
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("watchdog: could not open %s: %s", db_path, exc)
        return None
    try:
        try:
            row = conn.execute(
                "SELECT captured_at_utc FROM heartbeats ORDER BY captured_at_utc DESC LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # table not created yet (edge hasn't initialised the DB)
        if not row or not row[0]:
            return None
        ts = parse_iso_utc(str(row[0]))
        if ts is None:
            return None
        reference = now or datetime.now(timezone.utc)
        return max(0.0, (reference - ts).total_seconds())
    finally:
        conn.close()


async def watchdog_worker(config, state) -> None:
    """Poll edge liveness; restart the edge service when heartbeats go stale.

    Guarded by a boot grace (let the edge come up and write a first heartbeat)
    and a post-restart cooldown (don't restart-storm a service that's recovering).
    """
    if not config.watchdog_enabled:
        logger.info("Edge watchdog disabled by configuration.")
        return
    sup = BalenaSupervisor()
    logger.info(
        "Edge watchdog enabled: target=%s stale=%ss check=%ss cooldown=%ss boot_grace=%ss db=%s",
        config.watchdog_target_service,
        config.watchdog_stale_seconds,
        config.watchdog_check_interval_seconds,
        config.watchdog_restart_cooldown_seconds,
        config.watchdog_boot_grace_seconds,
        config.watchdog_db_path,
    )
    started_monotonic = time.monotonic()
    while True:
        await asyncio.sleep(config.watchdog_check_interval_seconds)
        # Boot grace: give the edge time to start and write its first heartbeat.
        if time.monotonic() - started_monotonic < config.watchdog_boot_grace_seconds:
            continue
        age = await asyncio.to_thread(newest_heartbeat_age_seconds, config.watchdog_db_path)
        state.watchdog_last_heartbeat_age = age
        if not watchdog_should_restart(age, stale_seconds=config.watchdog_stale_seconds):
            continue
        # Cooldown: a freshly-restarted service needs time to come back before we
        # judge it stale again.
        if state.watchdog_last_restart_monotonic is not None and (
            time.monotonic() - state.watchdog_last_restart_monotonic
            < config.watchdog_restart_cooldown_seconds
        ):
            continue
        logger.error(
            "Edge watchdog: no heartbeat for %ss (>= %ss) — restarting service '%s'.",
            int(age) if age is not None else "?",
            config.watchdog_stale_seconds,
            config.watchdog_target_service,
        )
        ok = await sup.restart_service(config.watchdog_target_service)
        state.watchdog_last_restart_monotonic = time.monotonic()
        state.watchdog_restart_count += 1
        if not ok:
            logger.warning(
                "Edge watchdog: restart call did not succeed (Supervisor API disabled or errored)."
            )
