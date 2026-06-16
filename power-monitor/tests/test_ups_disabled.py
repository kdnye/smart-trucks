import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("power_monitor_main", MODULE_PATH)
main = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(main)


class UpsDisabledTests(unittest.TestCase):
    def test_unset_keeps_ups_enabled(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(main._ups_disabled())

    def test_no_disables_ups(self) -> None:
        for value in ("no", "No", " NO ", "false", "0", "off", "none", "absent", "disabled", "n"):
            with mock.patch.dict(os.environ, {"UPS": value}, clear=True):
                self.assertTrue(main._ups_disabled(), msg=f"UPS={value!r} should disable")

    def test_affirmative_values_keep_ups_enabled(self) -> None:
        for value in ("yes", "y", "true", "1", "present", ""):
            with mock.patch.dict(os.environ, {"UPS": value}, clear=True):
                self.assertFalse(main._ups_disabled(), msg=f"UPS={value!r} should keep UPS enabled")


if __name__ == "__main__":
    unittest.main()
