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


class GpsPayloadCompatibilityTests(unittest.TestCase):
    def test_locked_fix_includes_alias_coordinates_and_speed_knots(self) -> None:
        payload = main.build_location_payload(
            {
                "fix_status": "locked",
                "latitude": 33.1,
                "longitude": -84.2,
                "speed_kmh": 37.04,
            }
        )

        self.assertEqual(payload["lat"], 33.1)
        self.assertEqual(payload["lng"], -84.2)
        self.assertEqual(payload["lon"], -84.2)
        self.assertAlmostEqual(payload["speed_knots"], 20.0, places=2)

    def test_searching_fix_does_not_emit_partial_aliases(self) -> None:
        payload = main.build_location_payload({"fix_status": "searching", "latitude": 10})
        self.assertEqual(payload, {"fix_status": "searching"})


if __name__ == "__main__":
    unittest.main()
