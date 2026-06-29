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

from shared.env import read_str_env

logger = logging.getLogger(__name__)

_VALID_TARGETS = {"start", "stop"}
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


class BalenaSupervisor:
    """Async client for the local Balena Supervisor v2 service-control API.

    The three ``BALENA_*`` variables are injected automatically when the
    docker-compose service carries ``io.balena.features.supervisor-api: '1'``.
    When they are absent (running off-Balena, or the label is missing) the
    client degrades to a logged no-op so the rest of telematics-edge keeps
    running — mirroring how ``network_watchdog_worker`` no-ops without ``nmcli``.
    """

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self._address = read_str_env("BALENA_SUPERVISOR_ADDRESS")
        self._api_key = read_str_env("BALENA_SUPERVISOR_API_KEY")
        # Balena injects BALENA_APP_ID; accept the SUPERVISOR-prefixed alias too.
        self._app_id = read_str_env("BALENA_SUPERVISOR_APP_ID") or read_str_env("BALENA_APP_ID")
        self._timeout_seconds = timeout_seconds
        self._enabled = bool(self._address and self._api_key and self._app_id)
        if not self._enabled:
            logger.warning(
                "Balena Supervisor API unavailable (missing address/key/app_id); "
                "Sentry Mode container control disabled (no-op). Action: add "
                "`io.balena.features.supervisor-api: '1'` to the telematics-edge "
                "service in docker-compose.yml."
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def stop_service(self, name: str) -> bool:
        return await self._post("stop", name)

    async def start_service(self, name: str) -> bool:
        return await self._post("start", name)

    async def set_services(self, names: Iterable[str], target: str) -> bool:
        """Apply ``target`` ("start"/"stop") to every service in ``names``.

        Every service is attempted even if an earlier one fails, so a partial
        failure still moves what it can and returns False for the caller to
        retry. Returns True only if all calls succeeded (vacuously True for an
        empty list). Returns False when the client is disabled.
        """
        if target not in _VALID_TARGETS:
            raise ValueError(f"target must be one of {_VALID_TARGETS}; got {target!r}")
        if not self._enabled:
            return False
        import aiohttp  # lazy: keeps module import light for unit tests / dev

        # Reuse one session across the (typically 2) service calls in a single
        # transition so the connection pool isn't rebuilt per request.
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        ok = True
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for name in names:
                if not await self._post(target, name, session=session):
                    ok = False
        return ok

    async def _post(self, action: str, name: str, session=None) -> bool:
        """POST one start/stop-service call. Reuses ``session`` when supplied,
        otherwise opens a short-lived one (for standalone start/stop calls)."""
        if not self._enabled:
            return False
        import aiohttp  # lazy: keeps module import light for unit tests / dev

        url = f"{self._address}/v2/applications/{self._app_id}/{action}-service"

        async def _send(sess) -> bool:
            try:
                # Auth is the ?apikey= query param (NOT a Bearer header), and the
                # body field is serviceName (the compose key, not imageName).
                async with sess.post(
                    url,
                    params={"apikey": self._api_key},
                    json={"serviceName": name},
                ) as response:
                    body = await response.text()
                    if 200 <= response.status < 300:
                        logger.info("Supervisor %s-service ok: service=%s", action, name)
                        return True
                    logger.warning(
                        "Supervisor %s-service failed: service=%s status=%s body=%s",
                        action,
                        name,
                        response.status,
                        body[:200].strip(),
                    )
                    return False
            except Exception as exc:  # aiohttp.ClientError / asyncio.TimeoutError / etc.
                logger.warning(
                    "Supervisor %s-service error: service=%s error=%s", action, name, exc
                )
                return False

        if session is not None:
            return await _send(session)
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as new_session:
            return await _send(new_session)


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
