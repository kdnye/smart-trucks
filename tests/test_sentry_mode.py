"""Tests for Sentry Mode software-sleep logic (``telematics-edge/sentry.py``).

Loads the import-light ``sentry`` module (stdlib + ``shared`` only) and exercises:

* the pure policy helpers ``should_suspend`` / ``next_action``,
* the ``SentryController`` suspend/resume edge state machine, which toggles the
  ``shared.sentry_flag`` sentinel that the ble-sensor / sync-service co-processes
  poll (one set per falling edge, one clear per rising edge, boot-safety clear).

(The Balena Supervisor client is now in ``shared/balena_supervisor.py`` and tested
in ``tests/test_balena_supervisor.py``; the sentinel helper in
``tests/test_sentry_flag.py``.) Runnable under pytest or standalone:

    python3 tests/test_sentry_mode.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODULE_PATH = REPO_ROOT / "telematics-edge" / "sentry.py"
SPEC = importlib.util.spec_from_file_location("sentry", MODULE_PATH)
sentry = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(sentry)


# --- pure helpers ---------------------------------------------------------


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
    assert sentry.next_action(True, False) == "suspend"
    assert sentry.next_action(True, True) is None
    assert sentry.next_action(False, True) == "resume"
    assert sentry.next_action(False, False) is None


# --- SentryController sentinel edge state machine --------------------------


def test_controller_full_suspend_resume_cycle():
    with tempfile.TemporaryDirectory() as tmp:
        flag = str(Path(tmp) / "sentry_suspend")
        ctrl = sentry.SentryController(flag)

        # boot-safety: clear any stale flag; nothing suspended at start
        ctrl.boot_safety_clear()
        assert not os.path.exists(flag)
        assert ctrl.prev_suspended is False

        # idle → suspend exactly once (sets the sentinel)
        assert ctrl.apply(True) == "suspend"
        assert os.path.exists(flag)
        assert ctrl.prev_suspended is True

        # still idle → no repeat (idempotent via prev_suspended shadow)
        assert ctrl.apply(True) is None
        assert os.path.exists(flag)

        # wake → resume exactly once (clears the sentinel)
        assert ctrl.apply(False) == "resume"
        assert not os.path.exists(flag)
        assert ctrl.prev_suspended is False

        # still awake → no repeat
        assert ctrl.apply(False) is None
        assert not os.path.exists(flag)


def test_boot_safety_clears_stale_flag():
    with tempfile.TemporaryDirectory() as tmp:
        flag = str(Path(tmp) / "sentry_suspend")
        # A crash/OTA mid-sleep left the sentinel on the persistent volume.
        with open(flag, "w", encoding="utf-8") as handle:
            handle.write("1")
        assert os.path.exists(flag)

        ctrl = sentry.SentryController(flag)
        ctrl.boot_safety_clear()
        assert not os.path.exists(flag)
        assert ctrl.prev_suspended is False


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
