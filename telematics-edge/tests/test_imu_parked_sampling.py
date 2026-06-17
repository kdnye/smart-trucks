import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "imu_reader.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("imu_reader", MODULE_PATH)
imu_reader = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(imu_reader)


class EffectiveSampleIntervalTests(unittest.TestCase):
    def test_active_uses_active_interval(self) -> None:
        interval = imu_reader.effective_sample_interval(
            parked=False,
            active_interval_seconds=0.1,
            parked_interval_seconds=1.0,
        )
        self.assertEqual(interval, 0.1)

    def test_parked_uses_parked_interval(self) -> None:
        interval = imu_reader.effective_sample_interval(
            parked=True,
            active_interval_seconds=0.1,
            parked_interval_seconds=1.0,
        )
        self.assertEqual(interval, 1.0)

    def test_parked_interval_never_faster_than_active(self) -> None:
        interval = imu_reader.effective_sample_interval(
            parked=True,
            active_interval_seconds=0.5,
            parked_interval_seconds=0.1,
        )
        self.assertEqual(interval, 0.5)


if __name__ == "__main__":
    unittest.main()
