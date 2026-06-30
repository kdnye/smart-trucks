"""Tests for shared/balena_supervisor.py.

Covers the no-op-without-credentials behaviour and target validation — the parts
that don't make HTTP calls (aiohttp is imported lazily only when enabled). Loaded
via importlib with REPO_ROOT on sys.path so `shared.env` resolves.
"""
import asyncio
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODULE_PATH = REPO_ROOT / "shared" / "balena_supervisor.py"
SPEC = importlib.util.spec_from_file_location("shared.balena_supervisor", MODULE_PATH)
bsup = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(bsup)

_BALENA_ENV = (
    "BALENA_SUPERVISOR_ADDRESS",
    "BALENA_SUPERVISOR_API_KEY",
    "BALENA_SUPERVISOR_APP_ID",
    "BALENA_APP_ID",
)


def test_disabled_without_credentials():
    import os

    saved = {k: os.environ.get(k) for k in _BALENA_ENV}
    try:
        for k in _BALENA_ENV:
            os.environ.pop(k, None)
        sup = bsup.BalenaSupervisor()
        assert sup.enabled is False
        # All control methods are no-ops returning False (no aiohttp import).
        assert asyncio.run(sup.stop_service("edge")) is False
        assert asyncio.run(sup.start_service("edge")) is False
        assert asyncio.run(sup.restart_service("edge")) is False
        assert asyncio.run(sup.set_services(["edge"], "restart")) is False
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_enabled_with_credentials():
    import os

    saved = {k: os.environ.get(k) for k in _BALENA_ENV}
    try:
        os.environ["BALENA_SUPERVISOR_ADDRESS"] = "http://127.0.0.1:48484"
        os.environ["BALENA_SUPERVISOR_API_KEY"] = "secret"
        os.environ["BALENA_APP_ID"] = "1234567"
        os.environ.pop("BALENA_SUPERVISOR_APP_ID", None)
        sup = bsup.BalenaSupervisor()
        assert sup.enabled is True
    finally:
        for k in _BALENA_ENV:
            os.environ.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_set_services_rejects_invalid_target():
    import os

    saved = {k: os.environ.get(k) for k in _BALENA_ENV}
    try:
        for k in _BALENA_ENV:
            os.environ.pop(k, None)
        sup = bsup.BalenaSupervisor()
        # Validation happens before the enabled check, so it raises even disabled.
        raised = False
        try:
            asyncio.run(sup.set_services(["edge"], "bogus"))
        except ValueError:
            raised = True
        assert raised, "expected ValueError for an invalid target"
        # "restart" is now a valid target.
        assert "restart" in bsup._VALID_TARGETS
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


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
