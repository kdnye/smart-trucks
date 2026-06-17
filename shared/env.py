"""Tolerant environment-variable parsing for edge services.

Balena's docker-compose interpolation does not expand the ``${VAR:-default}``
bash-style default syntax. When a variable is unset, the container receives the
*literal* string ``${VAR:-default}`` (or an empty string). Parsing that with a
bare ``int()``/``float()`` raises ``ValueError`` and crash-loops the service.

These helpers treat a missing, empty, or unexpanded ``${...}`` value as "unset"
and fall back to the supplied default. When the literal carries a ``:-`` body
(e.g. ``${BLE_POLL_INTERVAL_DISCHARGING_SECONDS:-300}``) the intended fallback
is recovered so the service still gets a sensible value.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def sanitize_env_value(raw_value: str | None) -> str | None:
    """Return a usable string, or ``None`` if missing/empty/unexpanded.

    Recovers the fallback from an unexpanded ``${VAR:-default}`` literal so a
    misconfigured Balena variable still yields its intended default.
    """
    if raw_value is None:
        return None

    value = raw_value.strip()
    if not value:
        return None

    if value.startswith("${") and value.endswith("}"):
        body = value[2:-1]
        if ":-" in body:
            _, fallback = body.split(":-", 1)
            fallback_value = fallback.strip()
            return fallback_value or None
        return None

    return value


def read_str_env(name: str, default: str | None = None) -> str | None:
    value = sanitize_env_value(os.getenv(name))
    return value if value is not None else default


def read_int_env(name: str, default: int, *, minimum: int | None = None) -> int:
    raw_value = os.getenv(name)
    value = sanitize_env_value(raw_value)
    if value is None:
        parsed = default
    else:
        try:
            parsed = int(value)
        except ValueError:
            logger.warning("Invalid %s=%r; using default %s.", name, raw_value, default)
            parsed = default
    return max(minimum, parsed) if minimum is not None else parsed


def read_float_env(name: str, default: float, *, minimum: float | None = None) -> float:
    raw_value = os.getenv(name)
    value = sanitize_env_value(raw_value)
    if value is None:
        parsed = default
    else:
        try:
            parsed = float(value)
        except ValueError:
            logger.warning("Invalid %s=%r; using default %s.", name, raw_value, default)
            parsed = default
    return max(minimum, parsed) if minimum is not None else parsed


def read_bool_env(name: str, default: bool = False) -> bool:
    value = sanitize_env_value(os.getenv(name))
    if value is None:
        return default
    lowered = value.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    logger.warning("Invalid %s=%r; using default %s.", name, value, default)
    return default
