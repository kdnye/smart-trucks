import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("ble_sensor_main", MODULE_PATH)
main = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(main)


def _config() -> "main.Config":
    # Empty environment -> documented defaults: POLL_INTERVAL=60, SCAN=20,
    # discharging=300, low_battery=900, low_soc=25, low_power_scan=10.
    return main.load_config()


class EffectiveScanCadenceTests(unittest.TestCase):
    def test_charging_uses_full_cadence(self) -> None:
        config = _config()
        interval, scan = main._effective_scan_cadence(
            config, {"soc_pct": 50, "is_charging": True}
        )
        self.assertEqual(interval, config.post_interval_seconds)
        self.assertEqual(scan, config.scan_duration_seconds)

    def test_unknown_power_uses_full_cadence(self) -> None:
        config = _config()
        interval, scan = main._effective_scan_cadence(config, None)
        self.assertEqual(interval, config.post_interval_seconds)
        self.assertEqual(scan, config.scan_duration_seconds)

    def test_discharging_normal_soc_throttles(self) -> None:
        config = _config()
        interval, scan = main._effective_scan_cadence(
            config, {"soc_pct": 80, "is_charging": False}
        )
        self.assertEqual(interval, config.poll_interval_discharging_seconds)
        self.assertEqual(scan, config.scan_duration_low_power_seconds)

    def test_discharging_low_soc_throttles_further(self) -> None:
        config = _config()
        interval, scan = main._effective_scan_cadence(
            config, {"soc_pct": 10, "is_charging": False}
        )
        self.assertEqual(interval, config.poll_interval_low_battery_seconds)
        self.assertEqual(scan, config.scan_duration_low_power_seconds)

    def test_malformed_soc_does_not_raise(self) -> None:
        config = _config()
        interval, scan = main._effective_scan_cadence(
            config, {"soc_pct": "n/a", "is_charging": False}
        )
        # Falls through to the conservative discharging cadence instead of crashing.
        self.assertEqual(interval, config.poll_interval_discharging_seconds)
        self.assertEqual(scan, config.scan_duration_low_power_seconds)


class ReadLatestPowerTests(unittest.TestCase):
    def test_missing_db_returns_none(self) -> None:
        result = main._read_latest_power_from_telematics_db("/nonexistent/x.db", 0.25)
        self.assertIsNone(result)

    def test_reads_latest_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "telematics.db")
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE power_readings ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, vehicle_id TEXT, "
                    "occurred_at TEXT, payload TEXT)"
                )
                conn.execute(
                    "INSERT INTO power_readings (vehicle_id, occurred_at, payload) VALUES (?, ?, ?)",
                    ("TRK", "2026-06-16T00:00:00+00:00",
                     json.dumps({"state_of_charge_pct_estimate": 60, "is_charging": False})),
                )
                conn.execute(
                    "INSERT INTO power_readings (vehicle_id, occurred_at, payload) VALUES (?, ?, ?)",
                    ("TRK", "2026-06-16T00:00:05+00:00",
                     json.dumps({"state_of_charge_pct_estimate": 42, "is_charging": True})),
                )

            result = main._read_latest_power_from_telematics_db(db_path, 0.25)

        self.assertEqual(result, {"soc_pct": 42, "is_charging": True})

    def test_no_table_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "telematics.db")
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE other (id INTEGER)")
            result = main._read_latest_power_from_telematics_db(db_path, 0.25)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
