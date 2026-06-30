"""Tests for shared/sentry_flag.py (the Sentry in-process suspend sentinel)."""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODULE_PATH = REPO_ROOT / "shared" / "sentry_flag.py"
SPEC = importlib.util.spec_from_file_location("shared.sentry_flag", MODULE_PATH)
sf = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(sf)


def test_flag_path_precedence():
    saved = os.environ.pop("SENTRY_SUSPEND_FLAG_PATH", None)
    try:
        # explicit arg wins
        assert sf.flag_path("/x/y") == "/x/y"
        # then env
        os.environ["SENTRY_SUSPEND_FLAG_PATH"] = "/env/path"
        assert sf.flag_path() == "/env/path"
        # then default
        os.environ.pop("SENTRY_SUSPEND_FLAG_PATH", None)
        assert sf.flag_path() == sf.DEFAULT_FLAG_PATH
    finally:
        if saved is not None:
            os.environ["SENTRY_SUSPEND_FLAG_PATH"] = saved


def test_set_clear_is_suspended_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "sentry_suspend")
        assert sf.is_suspended(path) is False
        sf.set_suspended(path)
        assert sf.is_suspended(path) is True
        # set is idempotent
        sf.set_suspended(path)
        assert sf.is_suspended(path) is True
        sf.clear_suspended(path)
        assert sf.is_suspended(path) is False
        # clearing a missing flag is a no-op (no exception)
        sf.clear_suspended(path)
        assert sf.is_suspended(path) is False


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
