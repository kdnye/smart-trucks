from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("telematics_edge_main", MODULE_PATH)
main = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(main)


class PowerMetricsPayloadCompatibilityTests(unittest.TestCase):
    def test_maps_bus_voltage_to_voltage_v(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        snapshot = {
            "occurred_at": now_iso,
            "payload": {
                "status": "ok",
                "bus_voltage_v": 3.91,
                "current_ma": -120.0,
            },
        }

        power_metrics, power_monitor_ok = main.build_power_metrics_payload(snapshot, max_snapshot_age_seconds=3600)

        self.assertEqual(power_metrics["voltage_v"], 3.91)
        self.assertEqual(power_metrics["bus_voltage_v"], 3.91)
        self.assertIs(power_metrics["snapshot_found"], True)
        self.assertIs(power_metrics["snapshot_stale"], False)
        self.assertIs(power_monitor_ok, True)

    def test_absent_snapshot_marks_status_absent(self) -> None:
        power_metrics, power_monitor_ok = main.build_power_metrics_payload(None, max_snapshot_age_seconds=3600)

        self.assertEqual(power_metrics["status"], "absent")
        self.assertIs(power_metrics["snapshot_found"], False)
        self.assertIs(power_monitor_ok, False)


if __name__ == "__main__":
    unittest.main()
