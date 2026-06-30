"""Sentry Mode: idle-driven software sleep via an in-process suspend sentinel.

When a truck has been idle for ``SENTRY_IDLE_TIMEOUT_SECONDS`` the master worker
puts the heavy work to sleep (the BLE radio + cloud sync) to cut power draw,
waking on a short cadence to watch for motion; any motion / charging / WiFi wake
resumes it. See ``docs/power-optimization.md``.

The BLE scan and cloud-upload loops run as co-processes in the same `edge`
container, so they can't be stopped via the Balena Supervisor API. Instead this
toggles a sentinel file on the shared `/data` volume (``shared.sentry_flag``)
that those loops poll and self-suspend on.

Deliberately import-light (stdlib + ``shared`` only): the decision logic and the
controller are unit-testable without the telematics-edge hardware import chain
(uvloop / smbus2 / aiosqlite / pyserial). The async orchestration that ties this
to GPS/IMU/heartbeats lives in ``main.py::sentry_mode_worker``.
"""

from __future__ import annotations

import logging

from shared import sentry_flag

logger = logging.getLogger(__name__)


def should_suspend(
    *,
    enabled: bool,
    truck_active: bool,
    is_charging: bool,
    wifi_connected: bool,
    idle_seconds: float,
    idle_timeout: float,
) -> bool:
    """Return True when the device should drop into Sentry sleep.

    Pure function (no I/O, no shared state) so the policy is trivially testable.
    Sleep requires idle past the timeout AND no wake signal — motion, charging,
    or WiFi connectivity. Including WiFi means a parked truck that regains WiFi
    wakes (and resumes uploads); it only sleeps when genuinely idle AND offline.
    Note it never reads ``parked_mode`` directly (it's transient); idleness is the
    caller-supplied ``idle_seconds``.
    """
    if not enabled:
        return False
    if truck_active or is_charging or wifi_connected:
        return False
    return idle_seconds >= idle_timeout


def next_action(desired_suspended: bool, prev_suspended: bool) -> str | None:
    """Return the single edge transition to perform, or None at steady state."""
    if desired_suspended and not prev_suspended:
        return "suspend"
    if not desired_suspended and prev_suspended:
        return "resume"
    return None


class SentryController:
    """Single owner of the suspend/resume edge decision.

    Holds the private ``prev_suspended`` shadow so exactly one suspend is issued
    per falling edge and one resume per rising edge, regardless of how many other
    workers toggle the shared ``parked_mode`` flag (which is transient, not
    latched, and so unsafe to edge-detect directly). The suspend signal is the
    ``shared.sentry_flag`` sentinel that the ble-sensor / sync-service
    co-processes poll.
    """

    def __init__(self, flag_path: str) -> None:
        self._flag_path = flag_path
        self.prev_suspended = False

    def boot_safety_clear(self) -> None:
        """On boot, clear any stale suspend sentinel a crash/OTA left mid-sleep so
        the co-processes don't start up already suspended (the file lives on the
        persistent /data volume)."""
        sentry_flag.clear_suspended(self._flag_path)
        self.prev_suspended = False

    def apply(self, desired_suspended: bool) -> str | None:
        """Set/clear the suspend sentinel on the single state transition.

        Returns "suspend"/"resume" on a transition, or None at steady state.
        """
        action = next_action(desired_suspended, self.prev_suspended)
        if action == "suspend":
            sentry_flag.set_suspended(self._flag_path)
            self.prev_suspended = True
            return "suspend"
        if action == "resume":
            sentry_flag.clear_suspended(self._flag_path)
            self.prev_suspended = False
            return "resume"
        return None
