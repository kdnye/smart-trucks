"""Shared on-disk sentinel for Sentry Mode's in-process co-process suspend.

Sentry Mode lives in telematics-edge, but the heavy power consumers it wants to
pause during sleep — the BLE scan loop and the cloud uploader — now run as
separate co-processes in the same `edge` container (not separate Balena
services), so they can't be stopped via the Supervisor API. Instead they
coordinate through a sentinel file:

  * telematics-edge SETS the flag when entering Sentry sleep, CLEARS it on wake,
    clears it on boot, and clears it when Sentry is disabled;
  * the ble-sensor and sync-service loops (also gated on SENTRY_MODE_ENABLED)
    poll ``is_suspended()`` and skip their work (idle the radio / stop uploading)
    while the flag is present.

The default path is **container-local** (``/run``), NOT the persistent ``/data``
volume: AGENTS.md forbids non-schema files on ``/data`` (they survive OTA), and a
"currently sleeping" flag must be ephemeral — it should never outlive the
container. All three co-processes share the edge container's filesystem, so a
container-local path is visible to all of them (the wifi-provisioner doesn't read
it).

Stdlib-only and import-light so every service can use it without extra deps.
Sentry Mode is off by default, so the flag never appears unless it's enabled.
"""

from __future__ import annotations

import logging
import os

from shared.env import sanitize_env_value

logger = logging.getLogger(__name__)

DEFAULT_FLAG_PATH = "/run/sentry_suspend"


def flag_path(override: str | None = None) -> str:
    """Resolve the sentinel path: explicit arg, then env, then default.

    All three co-processes call this with no arg so they agree on one path
    (they share the container's environment and the /data volume). Routed through
    sanitize_env_value so a Balena-passed literal ``${SENTRY_SUSPEND_FLAG_PATH:-
    /data/sentry_suspend}`` still resolves to its intended default."""
    return override or sanitize_env_value(os.getenv("SENTRY_SUSPEND_FLAG_PATH")) or DEFAULT_FLAG_PATH


def set_suspended(path: str) -> None:
    """Create the sentinel (presence is the signal). Best-effort; logs on failure."""
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("1")
    except OSError as exc:
        logger.warning("sentry_flag: could not set suspend flag %s: %s", path, exc)


def clear_suspended(path: str) -> None:
    """Remove the sentinel. A missing file is fine (already cleared)."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("sentry_flag: could not clear suspend flag %s: %s", path, exc)


def is_suspended(path: str) -> bool:
    return os.path.exists(path)
