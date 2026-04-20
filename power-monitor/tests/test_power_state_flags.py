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


class UpsMonitorSocTests(unittest.IsolatedAsyncioTestCase):
    async def test_voltage_curve_soc_at_3_9v_is_75_percent(self) -> None:
        monitor = main.UpsMonitor(
            i2c_addresses=(0x43,),
            shunt_ohms=0.01,
            max_expected_amps=4.0,
            gain_strategy="auto",
            bus_voltage_range_v=16,
            i2c_bus=1,
            i2c_scl_gpio_pin=3,
            battery_capacity_mah=10_000,
            min_discharge_current_ma_for_runtime=20,
        )
        monitor._ina = _FakeIna(
            {"status": "ok", "bus_voltage_v": 3.9, "current_ma": -100.0, "total_mAh": 8000.0}
        )

        payload = await monitor.read()

        self.assertEqual(payload["state_of_charge_pct_estimate"], 75)
        self.assertEqual(payload["estimate_method"], "voltage_curve_loaded")

    async def test_voltage_curve_soc_at_4_1v_is_100_percent(self) -> None:
        monitor = main.UpsMonitor(
            i2c_addresses=(0x43,),
            shunt_ohms=0.01,
            max_expected_amps=4.0,
            gain_strategy="auto",
            bus_voltage_range_v=16,
            i2c_bus=1,
            i2c_scl_gpio_pin=3,
            battery_capacity_mah=10_000,
            min_discharge_current_ma_for_runtime=20,
        )
        monitor._ina = _FakeIna(
            {"status": "ok", "bus_voltage_v": 4.1, "current_ma": -100.0, "total_mAh": 8000.0}
        )

        payload = await monitor.read()

        self.assertEqual(payload["state_of_charge_pct_estimate"], 100)
        self.assertEqual(payload["estimate_method"], "voltage_curve_loaded")

    async def test_coulomb_soc_is_used_when_bus_voltage_is_5v(self) -> None:
        monitor = main.UpsMonitor(
            i2c_addresses=(0x43,),
            shunt_ohms=0.01,
            max_expected_amps=4.0,
            gain_strategy="auto",
            bus_voltage_range_v=16,
            i2c_bus=1,
            i2c_scl_gpio_pin=3,
            battery_capacity_mah=10_000,
            min_discharge_current_ma_for_runtime=20,
        )
        monitor._ina = _FakeIna(
            {"status": "ok", "bus_voltage_v": 5.0, "current_ma": -100.0, "total_mAh": 8000.0}
        )

        payload = await monitor.read()

        self.assertEqual(payload["state_of_charge_pct_estimate"], 80)
        self.assertEqual(payload["estimate_method"], "coulomb_counter_output_rail")

    async def test_invert_current_marks_positive_load_current_as_discharging(self) -> None:
        monitor = main.UpsMonitor(
            i2c_addresses=(0x43,),
            shunt_ohms=0.01,
            max_expected_amps=4.0,
            gain_strategy="auto",
            bus_voltage_range_v=16,
            i2c_bus=1,
            i2c_scl_gpio_pin=3,
            battery_capacity_mah=10_000,
            min_discharge_current_ma_for_runtime=20,
            invert_current=True,
        )
        monitor._ina = _FakeIna(
            {"status": "ok", "bus_voltage_v": 5.0, "current_ma": -180.0, "total_mAh": 8000.0}
        )

        payload = await monitor.read()

        self.assertFalse(payload["is_charging"])
        self.assertLess(payload["current_ma"], 0.0)


class _FakeIna:
    def __init__(self, payload: dict[str, float | str]) -> None:
        self._payload = payload

    async def read(self) -> dict[str, float | str]:
        return dict(self._payload)


if __name__ == "__main__":
    unittest.main()
