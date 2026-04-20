import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("telematics_edge_main", MODULE_PATH)
main = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(main)


class WarehouseModeTests(unittest.TestCase):
    def test_load_config_requires_warehouse_coordinates(self) -> None:
        env = {
            "DEVICE_ROLE": "warehouse",
            "WAREHOUSE_LAT": "",
            "WAREHOUSE_LON": "",
        }
        with patch.dict(main.os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                main.load_config()

    def test_get_latest_gps_uses_warehouse_coordinates(self) -> None:
        config = main.Config(
            vehicle_id="WH-01",
            device_role="warehouse",
            webhook_url=None,
            api_key="",
            gps_sample_interval_seconds=5,
            heartbeat_interval_seconds=60,
            sync_interval_seconds=20,
            gps_serial_candidates=("tcp://gps-multiplexer:2947",),
            gps_probe_all_candidates=False,
            gps_baud_rate=9600,
            sync_batch_size=50,
            sync_backoff_max_seconds=300,
            queue_alert_depth=1000,
            power_snapshot_max_age_seconds=30,
            imu_i2c_bus=1,
            imu_expected_addresses=(0x6A,),
            imu_required=True,
            warehouse_latitude=40.7128,
            warehouse_longitude=-74.0060,
        )
        state = main.RuntimeState(start_monotonic=0.0)
        state.latest_valid_gps = {
            "fix_status": "locked",
            "latitude": 1.0,
            "longitude": 2.0,
        }

        gps = main.get_latest_gps(config, state)
        self.assertEqual(gps["fix_status"], "locked")
        self.assertEqual(gps["latitude"], 40.7128)
        self.assertEqual(gps["longitude"], -74.0060)
        self.assertEqual(gps["device"], "warehouse_config")


if __name__ == "__main__":
    unittest.main()
