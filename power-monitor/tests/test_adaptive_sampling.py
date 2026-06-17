import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("power_monitor_main", MODULE_PATH)
main = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(main)


def _config() -> "main.Config":
    # Defaults are read from the environment; an empty environment yields the
    # documented defaults (fast=2s, idle=20s, soc_margin=15, voltage_margin=0.20,
    # soc_trip=10, voltage_trip=3.50).
    return main.load_config()


class AdaptiveSamplingTests(unittest.TestCase):
    def test_healthy_battery_backs_off_to_idle_interval(self) -> None:
        config = _config()
        payload = {"status": "ok", "state_of_charge_pct_estimate": 90, "bus_voltage_v": 4.0}
        interval = main._select_next_sample_interval(payload, config, charging_changed=False)
        self.assertEqual(interval, config.sample_interval_idle_seconds)

    def test_soc_near_trip_stays_fast(self) -> None:
        config = _config()
        # trip 10 + margin 15 = 24 -> SOC 20 is within the fast band.
        payload = {"status": "ok", "state_of_charge_pct_estimate": 20, "bus_voltage_v": 3.9}
        interval = main._select_next_sample_interval(payload, config, charging_changed=False)
        self.assertEqual(interval, config.sample_interval_seconds)

    def test_voltage_near_trip_stays_fast(self) -> None:
        config = _config()
        # trip 3.50 + margin 0.20 = 3.70 -> 3.65 is within the fast band.
        payload = {"status": "ok", "state_of_charge_pct_estimate": 90, "bus_voltage_v": 3.65}
        interval = main._select_next_sample_interval(payload, config, charging_changed=False)
        self.assertEqual(interval, config.sample_interval_seconds)

    def test_charging_change_stays_fast(self) -> None:
        config = _config()
        payload = {"status": "ok", "state_of_charge_pct_estimate": 90, "bus_voltage_v": 4.0}
        interval = main._select_next_sample_interval(payload, config, charging_changed=True)
        self.assertEqual(interval, config.sample_interval_seconds)

    def test_sensor_fault_stays_fast(self) -> None:
        config = _config()
        payload = {"status": "read_error", "state_of_charge_pct_estimate": 90, "bus_voltage_v": 4.0}
        interval = main._select_next_sample_interval(payload, config, charging_changed=False)
        self.assertEqual(interval, config.sample_interval_seconds)


if __name__ == "__main__":
    unittest.main()
