"""Tests for Sentry Mode software-sleep logic (``telematics-edge/sentry.py``).

Loads the import-light ``sentry`` module — its decision logic and Supervisor
client need no aiohttp / uvloop / hardware (aiohttp is imported lazily only when
an actual HTTP call is made) — and exercises:

* the pure policy helpers ``parse_service_list`` / ``should_suspend`` / ``next_action``,
* the ``BalenaSupervisor`` graceful no-op when Supervisor creds are absent,
* the ``SentryController`` stop/start edge state machine via a fake supervisor
  (one stop per falling edge, one start per rising edge, self-healing retry on
  failure, boot-safety start, config-driven pause-set).

The full async worker (announce + flush grace) is integration-tested on-device;
here we cover the deterministic logic. Runnable under pytest or standalone:

    python3 tests/test_sentry_mode.py
"""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODULE_PATH = REPO_ROOT / "telematics-edge" / "sentry.py"
SPEC = importlib.util.spec_from_file_location("sentry", MODULE_PATH)
sentry = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(sentry)


class FakeSupervisor:
    """Records set_services calls; configurable enabled-state and failure modes."""

    def __init__(self, *, enabled: bool = True, fail_targets=()) -> None:
        self.enabled = enabled
        self._fail = set(fail_targets)
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def set_services(self, names, target):
        self.calls.append((target, tuple(names)))
        if not self.enabled:
            return False
        return target not in self._fail


# --- pure helpers ---------------------------------------------------------


def test_parse_service_list():
    assert sentry.parse_service_list("ble-sensor,sync-service") == ("ble-sensor", "sync-service")
    # trims whitespace
    assert sentry.parse_service_list(" ble-sensor , sync-service ") == ("ble-sensor", "sync-service")
    # dedupes, preserving order
    assert sentry.parse_service_list("ble-sensor,ble-sensor,sync-service") == ("ble-sensor", "sync-service")
    # never suspends the container running this code
    assert sentry.parse_service_list("ble-sensor,telematics-edge") == ("ble-sensor",)
    # single-service config (keep cloud visibility) is honored
    assert sentry.parse_service_list("ble-sensor") == ("ble-sensor",)
    assert sentry.parse_service_list("") == ()
    assert sentry.parse_service_list(None) == ()


def test_should_suspend():
    base = dict(enabled=True, truck_active=False, is_charging=False, idle_seconds=400, idle_timeout=300)
    assert sentry.should_suspend(**base) is True
    assert sentry.should_suspend(**{**base, "enabled": False}) is False
    assert sentry.should_suspend(**{**base, "truck_active": True}) is False
    assert sentry.should_suspend(**{**base, "is_charging": True}) is False
    # under the idle timeout → stay awake
    assert sentry.should_suspend(**{**base, "idle_seconds": 100}) is False
    # exactly at the timeout → suspend
    assert sentry.should_suspend(**{**base, "idle_seconds": 300}) is True


def test_next_action():
    assert sentry.next_action(True, False) == "stop"
    assert sentry.next_action(True, True) is None
    assert sentry.next_action(False, True) == "start"
    assert sentry.next_action(False, False) is None


# --- SentryController edge state machine -----------------------------------


def test_controller_full_idle_wake_cycle():
    async def _run():
        sup = FakeSupervisor()
        ctrl = sentry.SentryController(sup, ("ble-sensor", "sync-service"))
        # boot-safety: one start so a crash/OTA mid-sleep can't strand containers
        await ctrl.boot_safety_start()
        assert sup.calls == [("start", ("ble-sensor", "sync-service"))]
        assert ctrl.prev_paused is False
        sup.calls.clear()

        # idle → suspend exactly once
        assert await ctrl.apply(True) == "stop"
        assert ctrl.prev_paused is True
        assert sup.calls == [("stop", ("ble-sensor", "sync-service"))]

        # still idle → no repeat stop (idempotent via prev_paused shadow)
        assert await ctrl.apply(True) is None
        assert len(sup.calls) == 1

        # wake → resume exactly once
        assert await ctrl.apply(False) == "start"
        assert ctrl.prev_paused is False
        assert sup.calls[-1] == ("start", ("ble-sensor", "sync-service"))

        # still awake → no repeat start
        assert await ctrl.apply(False) is None
        assert len(sup.calls) == 2

    asyncio.run(_run())


def test_controller_failed_stop_retries():
    async def _run():
        sup = FakeSupervisor(fail_targets={"stop"})
        ctrl = sentry.SentryController(sup, ("ble-sensor",))
        await ctrl.boot_safety_start()
        sup.calls.clear()

        # stop fails → shadow NOT advanced → next poll retries
        assert await ctrl.apply(True) is None
        assert ctrl.prev_paused is False
        assert await ctrl.apply(True) is None
        assert sup.calls == [("stop", ("ble-sensor",)), ("stop", ("ble-sensor",))]

    asyncio.run(_run())


def test_controller_disabled_supervisor_clears_shadow_on_wake():
    async def _run():
        sup = FakeSupervisor(enabled=False)
        ctrl = sentry.SentryController(sup, ("ble-sensor",))
        # stop is a no-op failure → shadow stays False
        assert await ctrl.apply(True) is None
        assert ctrl.prev_paused is False
        # if the shadow were set, a wake clears it even though start is a no-op,
        # so we don't loop forever "starting" on a non-Balena box
        ctrl.prev_paused = True
        assert await ctrl.apply(False) == "start"
        assert ctrl.prev_paused is False

    asyncio.run(_run())


def test_controller_empty_pause_set_makes_no_calls():
    async def _run():
        sup = FakeSupervisor()
        ctrl = sentry.SentryController(sup, ())
        await ctrl.boot_safety_start()
        assert sup.calls == []
        assert await ctrl.apply(True) is None
        assert sup.calls == []

    asyncio.run(_run())


def test_controller_single_service_pause_set():
    async def _run():
        sup = FakeSupervisor()
        ctrl = sentry.SentryController(sup, ("ble-sensor",))
        await ctrl.boot_safety_start()
        sup.calls.clear()
        await ctrl.apply(True)
        assert sup.calls == [("stop", ("ble-sensor",))]

    asyncio.run(_run())


# --- BalenaSupervisor graceful degradation ---------------------------------

_BALENA_ENV_KEYS = (
    "BALENA_SUPERVISOR_ADDRESS",
    "BALENA_SUPERVISOR_API_KEY",
    "BALENA_SUPERVISOR_APP_ID",
    "BALENA_APP_ID",
)


def test_balena_supervisor_noop_without_creds():
    async def _run():
        saved = {k: os.environ.pop(k, None) for k in _BALENA_ENV_KEYS}
        try:
            sup = sentry.BalenaSupervisor()
            assert sup.enabled is False
            # every method no-ops (returns False) without attempting a request
            assert await sup.set_services(["ble-sensor"], "stop") is False
            assert await sup.stop_service("ble-sensor") is False
            assert await sup.start_service("ble-sensor") is False
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    asyncio.run(_run())


def test_balena_supervisor_enabled_with_creds():
    saved = {k: os.environ.get(k) for k in _BALENA_ENV_KEYS}
    os.environ["BALENA_SUPERVISOR_ADDRESS"] = "http://127.0.0.1:48484"
    os.environ["BALENA_SUPERVISOR_API_KEY"] = "secret"
    os.environ["BALENA_APP_ID"] = "1234567"
    os.environ.pop("BALENA_SUPERVISOR_APP_ID", None)
    try:
        sup = sentry.BalenaSupervisor()
        assert sup.enabled is True
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_balena_supervisor_rejects_invalid_target():
    async def _run():
        sup = sentry.BalenaSupervisor()
        raised = False
        try:
            await sup.set_services(["ble-sensor"], "pause")
        except ValueError:
            raised = True
        assert raised, "expected ValueError for an invalid target"

    asyncio.run(_run())


def _run_standalone() -> int:
    tests = sorted(name for name in globals() if name.startswith("test_"))
    failures = 0
    for name in tests:
        try:
            globals()[name]()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001 - standalone runner surface
            failures += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
