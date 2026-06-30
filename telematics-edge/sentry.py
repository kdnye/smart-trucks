"""Sentry Mode: idle-driven container suspension via the Balena Supervisor API.

When a truck has been idle for ``SENTRY_IDLE_TIMEOUT_SECONDS`` the master worker
stops the heavy containers (BLE radio + cloud sync) to cut power draw, waking on
a short cadence to watch for motion; any motion / charging / WiFi wake restarts
them. See ``docs/power-optimization.md``.

This module is deliberately import-light: only the standard library,
``shared.env``, and — lazily, inside the one method that makes HTTP calls —
``aiohttp``. That keeps the decision logic and the Supervisor client
unit-testable without the telematics-edge hardware import chain
(uvloop / smbus2 / aiosqlite / pyserial). The async orchestration that ties this
to GPS/IMU/heartbeats lives in ``main.py::sentry_mode_worker``.
"""

from __future__ import annotations

import logging
from typing import Iterable

# BalenaSupervisor moved to shared/ so the wifi-provisioner watchdog can reuse it.
# Re-exported here for backward compatibility (sentry.BalenaSupervisor, tests).
from shared.balena_supervisor import BalenaSupervisor

logger = logging.getLogger(__name__)

# Never suspend the container that runs this code.
SELF_SERVICE_NAME = "telematics-edge"


def parse_service_list(raw: str | None) -> tuple[str, ...]:
    """Parse a comma-separated service list: trimmed, deduped, self-excluded.

    ``telematics-edge`` is filtered out defensively — suspending the container
    that runs Sentry Mode would deadlock the wake path.
    """
    seen: dict[str, None] = {}
    for name in (raw or "").split(","):
        name = name.strip()
        if name and name != SELF_SERVICE_NAME:
            seen.setdefault(name, None)
    return tuple(seen)


def should_suspend(
    *,
    enabled: bool,
    truck_active: bool,
    is_charging: bool,
    idle_seconds: float,
    idle_timeout: float,
) -> bool:
    """Return True when the device should drop into Sentry sleep.

    Pure function (no I/O, no shared state) so the policy is trivially testable.
    Note it never reads ``parked_mode``: idleness is computed directly, so a
    WiFi-loss park (which also sets ``parked_mode``) does NOT trigger container
    suspension — only genuine idle-timeout does.
    """
    if not enabled:
        return False
    if truck_active or is_charging:
        return False
    return idle_seconds >= idle_timeout


def next_action(desired_suspended: bool, prev_paused: bool) -> str | None:
    """Return the single edge transition to perform, or None at steady state."""
    if desired_suspended and not prev_paused:
        return "stop"
    if not desired_suspended and prev_paused:
        return "start"
    return None


class SentryController:
    """Single owner of the suspend/resume edge decision.

    Holds the private ``prev_paused`` shadow so exactly one stop is issued per
    falling edge and one start per rising edge, regardless of how many other
    workers toggle the shared ``parked_mode`` flag (which is transient, not
    latched, and so unsafe to edge-detect directly). On a failed supervisor call
    the shadow is NOT advanced, so the next poll retries (self-healing).
    """

    def __init__(self, supervisor: BalenaSupervisor, pause_services: Iterable[str]) -> None:
        self._sup = supervisor
        self._services = tuple(pause_services)
        self.prev_paused = False

    async def boot_safety_start(self) -> None:
        """On boot, ensure the pause-set containers are running (idempotent).

        Balena persists a stopped service's target state across a telematics-edge
        restart, so a crash / OTA mid-sleep would otherwise strand them stopped.
        """
        if self._services:
            await self._sup.set_services(self._services, "start")
        self.prev_paused = False

    async def apply(self, desired_suspended: bool) -> str | None:
        """Issue at most one transition for the desired level.

        Returns the action actually performed ("stop"/"start"), or None when at
        steady state, when there are no services to manage, or when the call
        failed (the shadow is left unchanged so the next poll retries).
        """
        action = next_action(desired_suspended, self.prev_paused)
        if action is None or not self._services:
            return None
        ok = await self._sup.set_services(self._services, action)
        if action == "stop":
            if ok:
                self.prev_paused = True
                return "stop"
            return None
        # start: clear the shadow on success, or when the supervisor is a no-op
        # (no creds / off-Balena) so we don't loop forever "starting" on a dev box.
        if ok or not self._sup.enabled:
            self.prev_paused = False
            return "start"
        return None
