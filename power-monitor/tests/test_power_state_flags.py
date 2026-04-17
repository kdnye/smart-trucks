import importlib.util
from pathlib import Path
import sys
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("power_monitor_main", MODULE_PATH)
main = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(main)


class PowerStateFlagsTests(unittest.TestCase):
    def test_unplugged_low_positive_current_is_not_charging(self) -> None:
        payload = {"status": "ok", "bus_voltage_v": 3.9, "current_ma": 8.0}

        flags = main._derive_power_flags(payload)

        self.assertFalse(flags["external_input_present"])
        self.assertFalse(flags["charging"])
        self.assertTrue(flags["battery_only"])

    def test_external_power_with_charge_current_is_charging(self) -> None:
        payload = {"status": "ok", "bus_voltage_v": 5.05, "current_ma": 180.0}

        flags = main._derive_power_flags(payload)

        self.assertTrue(flags["external_input_present"])
        self.assertTrue(flags["charging"])
        self.assertFalse(flags["battery_only"])


if __name__ == "__main__":
    unittest.main()
