import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_imu_calibration_truth_table.py"
SPEC = importlib.util.spec_from_file_location("sync_truth_table", MODULE_PATH)
sync_truth_table = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(sync_truth_table)


class SyncImuCalibrationTruthTableTests(unittest.TestCase):
    def test_build_truth_table_summarizes_captured_windows(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "imu-calibration.db"
            output_path = Path(tmp_dir) / "truth.json"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE calibration_marks (id INTEGER PRIMARY KEY, session_id INTEGER, event_type TEXT, custom_label TEXT, sample_index INTEGER)"
            )
            conn.execute(
                "CREATE TABLE imu_samples (id INTEGER PRIMARY KEY, session_id INTEGER, sample_index INTEGER, magnitude_3d REAL, magnitude_2d REAL, gyro_x REAL, gyro_y REAL, gyro_z REAL)"
            )

            for sample_index, value in enumerate([1.0, 1.1, 1.2, 2.0, 2.1], start=1):
                conn.execute(
                    "INSERT INTO imu_samples(session_id, sample_index, magnitude_3d, magnitude_2d, gyro_x, gyro_y, gyro_z) VALUES(1, ?, ?, ?, 0.1, 0.1, 0.1)",
                    (sample_index, value, value - 0.2),
                )
            conn.execute(
                "INSERT INTO calibration_marks(session_id, event_type, custom_label, sample_index) VALUES(1, 'speed_bump', '', 3)"
            )
            conn.commit()
            conn.close()

            sync_truth_table.build_truth_table(db_path, output_path, window_samples=1)
            payload = json.loads(output_path.read_text(encoding="utf-8"))

            self.assertIn("speed_bump", payload["event_profiles"])
            profile = payload["event_profiles"]["speed_bump"]
            self.assertEqual(profile["sample_count"], 3)
            self.assertAlmostEqual(profile["magnitude_3d"]["mean"], 1.433333, places=6)


if __name__ == "__main__":
    unittest.main()
