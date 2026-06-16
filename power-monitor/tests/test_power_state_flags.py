import importlib.util
import tempfile
from pathlib import Path
import aiosqlite
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
    async def test_voltage_curve_soc_at_3_9v_uses_resting_curve(self) -> None:
        monitor = main.UpsMonitor(
            i2c_addresses=(0x43,),
            shunt_ohms=0.01,
            max_expected_amps=4.0,
            gain_strategy="auto",
            bus_voltage_range_v=16,
            i2c_bus=1,
            i2c_scl_gpio_pin=3,
            battery_capacity_mah=10_000,
            soc_voltage_min_v=3.2,
            soc_voltage_full_resting_v=4.05,
            soc_voltage_full_charging_v=4.25,
            min_discharge_current_ma_for_runtime=20,
        )
        monitor._ina = _FakeIna(
            {"status": "ok", "bus_voltage_v": 3.9, "current_ma": -100.0, "total_mAh": 8000.0}
        )

        payload = await monitor.read()

        self.assertEqual(payload["state_of_charge_pct_estimate"], 82)
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
            soc_voltage_min_v=3.2,
            soc_voltage_full_resting_v=4.05,
            soc_voltage_full_charging_v=4.25,
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
            soc_voltage_min_v=3.2,
            soc_voltage_full_resting_v=4.05,
            soc_voltage_full_charging_v=4.25,
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
            soc_voltage_min_v=3.2,
            soc_voltage_full_resting_v=4.05,
            soc_voltage_full_charging_v=4.25,
            min_discharge_current_ma_for_runtime=20,
            invert_current=True,
        )
        monitor._ina = _FakeIna(
            {"status": "ok", "bus_voltage_v": 5.0, "current_ma": -180.0, "total_mAh": 8000.0}
        )

        payload = await monitor.read()

        self.assertFalse(payload["is_charging"])
        self.assertLess(payload["current_ma"], 0.0)

    async def test_charging_curve_uses_higher_full_voltage(self) -> None:
        monitor = main.UpsMonitor(
            i2c_addresses=(0x43,),
            shunt_ohms=0.01,
            max_expected_amps=4.0,
            gain_strategy="auto",
            bus_voltage_range_v=16,
            i2c_bus=1,
            i2c_scl_gpio_pin=3,
            battery_capacity_mah=10_000,
            soc_voltage_min_v=3.2,
            soc_voltage_full_resting_v=4.05,
            soc_voltage_full_charging_v=4.25,
            min_discharge_current_ma_for_runtime=20,
        )
        monitor._ina = _FakeIna(
            {"status": "ok", "bus_voltage_v": 4.05, "current_ma": 150.0, "total_mAh": 8000.0}
        )

        payload = await monitor.read()

        self.assertFalse(payload["is_charging"])
        self.assertEqual(payload["state_of_charge_pct_estimate"], 81)


class PowerMonitorSqliteTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_db_creates_power_readings_without_local_power(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "power.db"
            async with aiosqlite.connect(db_path) as conn:
                await main.configure_sqlite(conn)
                await main.init_db(conn)

                async with conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                ) as cursor:
                    rows = await cursor.fetchall()

        table_names = {str(row[0]) for row in rows}
        self.assertIn("power_readings", table_names)
        self.assertNotIn("local_power", table_names)

    async def test_insert_power_reading_matches_latest_snapshot_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "power.db"
            async with aiosqlite.connect(db_path) as conn:
                await main.configure_sqlite(conn)
                await main.init_db(conn)
                payload = {
                    "status": "ok",
                    "bus_voltage_v": 4.1,
                    "state_of_charge_pct_estimate": 88,
                    "power_state": "charging",
                }

                await main.insert_power_reading(
                    conn,
                    vehicle_id="TRK-TEST",
                    occurred_at="2026-06-16T00:00:00+00:00",
                    payload=payload,
                )

                async with conn.execute(
                    "SELECT occurred_at, payload FROM power_readings WHERE vehicle_id = ?",
                    ("TRK-TEST",),
                ) as cursor:
                    row = await cursor.fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], "2026-06-16T00:00:00+00:00")
        self.assertIn('"state_of_charge_pct_estimate":88', row[1])


class _FakeIna:
    def __init__(self, payload: dict[str, float | str]) -> None:
        self._payload = payload

    async def read(self) -> dict[str, float | str]:
        return dict(self._payload)


if __name__ == "__main__":
    unittest.main()
