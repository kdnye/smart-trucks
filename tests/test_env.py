"""Tests for shared.env tolerant environment-variable parsing.

No hardware or network. Runnable either under pytest
(``python3 -m pytest tests/test_env.py``) or standalone
(``python3 tests/test_env.py``).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from shared.env import read_bool_env, read_float_env, read_int_env  # noqa: E402


def _set(monkey_name, value):
    if value is None:
        os.environ.pop(monkey_name, None)
    else:
        os.environ[monkey_name] = value


def test_read_int_env_missing():
    _set("ST_TEST_INT", None)
    assert read_int_env("ST_TEST_INT", 300) == 300


def test_read_int_env_empty():
    _set("ST_TEST_INT", "")
    assert read_int_env("ST_TEST_INT", 300) == 300


def test_read_int_env_valid():
    _set("ST_TEST_INT", "42")
    assert read_int_env("ST_TEST_INT", 300) == 42


def test_read_int_env_garbage():
    _set("ST_TEST_INT", "not-a-number")
    assert read_int_env("ST_TEST_INT", 300) == 300


def test_read_int_env_unexpanded_literal_recovers_default():
    # Balena leaves ${VAR:-300} unexpanded when VAR is unset; the helper must
    # recover the 300 fallback rather than crashing.
    _set("ST_TEST_INT", "${SOME_VAR:-300}")
    assert read_int_env("ST_TEST_INT", 7) == 300


def test_read_int_env_unexpanded_no_default():
    _set("ST_TEST_INT", "${SOME_VAR}")
    assert read_int_env("ST_TEST_INT", 7) == 7


def test_read_int_env_minimum_clamp():
    _set("ST_TEST_INT", "0")
    assert read_int_env("ST_TEST_INT", 50, minimum=1) == 1


def test_read_float_env_missing():
    _set("ST_TEST_FLOAT", None)
    assert read_float_env("ST_TEST_FLOAT", 1.5) == 1.5


def test_read_float_env_empty():
    _set("ST_TEST_FLOAT", "")
    assert read_float_env("ST_TEST_FLOAT", 1.5) == 1.5


def test_read_float_env_valid():
    _set("ST_TEST_FLOAT", "0.4")
    assert read_float_env("ST_TEST_FLOAT", 1.5) == 0.4


def test_read_float_env_garbage():
    _set("ST_TEST_FLOAT", "abc")
    assert read_float_env("ST_TEST_FLOAT", 1.5) == 1.5


def test_read_float_env_unexpanded_literal_recovers_default():
    _set("ST_TEST_FLOAT", "${SOME_VAR:-2.5}")
    assert read_float_env("ST_TEST_FLOAT", 9.9) == 2.5


def test_read_bool_env_missing():
    _set("ST_TEST_BOOL", None)
    assert read_bool_env("ST_TEST_BOOL", default=True) is True
    assert read_bool_env("ST_TEST_BOOL", default=False) is False


def test_read_bool_env_empty():
    _set("ST_TEST_BOOL", "")
    assert read_bool_env("ST_TEST_BOOL", default=True) is True


def test_read_bool_env_valid():
    for truthy in ("1", "true", "yes", "on", "TRUE"):
        _set("ST_TEST_BOOL", truthy)
        assert read_bool_env("ST_TEST_BOOL") is True
    for falsy in ("0", "false", "no", "off", "FALSE"):
        _set("ST_TEST_BOOL", falsy)
        assert read_bool_env("ST_TEST_BOOL", default=True) is False


def test_read_bool_env_garbage():
    _set("ST_TEST_BOOL", "maybe")
    assert read_bool_env("ST_TEST_BOOL", default=True) is True


def test_read_bool_env_unexpanded_literal_recovers_default():
    _set("ST_TEST_BOOL", "${SOME_VAR:-true}")
    assert read_bool_env("ST_TEST_BOOL", default=False) is True


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
