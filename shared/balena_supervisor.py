"""Async client for the local Balena Supervisor v2 service-control API.

Shared by ``telematics-edge`` (Sentry Mode) and ``wifi-provisioner`` (the
cross-service watchdog), so it lives in ``shared/`` rather than inside either
service. Import-light: only the standard library and ``shared.env`` at module
load; ``aiohttp`` is imported lazily inside the methods that make HTTP calls,
so the decision logic stays unit-testable without a running Supervisor.

The three ``BALENA_*`` variables are injected automatically when a
docker-compose service carries ``io.balena.features.supervisor-api: '1'``. When
they are absent (running off-Balena, or the label is missing) the client
degrades to a logged no-op so callers keep running.
"""

from __future__ import annotations

import logging
from typing import Iterable

from shared.env import read_str_env

logger = logging.getLogger(__name__)

_VALID_TARGETS = {"start", "stop", "restart"}


class BalenaSupervisor:
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
                "service control disabled (no-op). Action: add "
                "`io.balena.features.supervisor-api: '1'` to the service in "
                "docker-compose.yml."
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def stop_service(self, name: str) -> bool:
        return await self._post("stop", name)

    async def start_service(self, name: str) -> bool:
        return await self._post("start", name)

    async def restart_service(self, name: str) -> bool:
        """Restart one service via the Supervisor ``restart-service`` endpoint."""
        return await self._post("restart", name)

    async def set_services(self, names: Iterable[str], target: str) -> bool:
        """Apply ``target`` ("start"/"stop"/"restart") to every service in ``names``.

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

        # Reuse one session across the service calls in a single transition so
        # the connection pool isn't rebuilt per request.
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        ok = True
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for name in names:
                if not await self._post(target, name, session=session):
                    ok = False
        return ok

    async def _post(self, action: str, name: str, session=None) -> bool:
        """POST one start/stop/restart-service call. Reuses ``session`` when
        supplied, otherwise opens a short-lived one."""
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
