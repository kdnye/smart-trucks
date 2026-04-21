import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
SPEC = importlib.util.spec_from_file_location("ble_calibration_main", MODULE_PATH)
main = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(main)


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class BleCalibrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_db_path = main.DB_PATH
        main.DB_PATH = str(Path(self.tmpdir.name) / "ble-calibration.db")
        main.state.session_id = None
        main.state.session_label = None
        main.state.grid_width = 8
        main.state.grid_height = 8
        main.state.pi_x = 0
        main.state.pi_y = 0
        main.state.obstructions = set()
        main.state.beacons = {}
        main.state.calibration_models = {}
        await main.init_db()

    async def asyncTearDown(self) -> None:
        main.DB_PATH = self.original_db_path
        self.tmpdir.cleanup()

    async def test_fit_path_loss_returns_nonzero_for_varied_distances(self):
        samples = [(1.0, -45), (2.0, -52), (4.0, -60), (8.0, -68)]
        result = main.fit_path_loss(samples)
        self.assertLess(result["slope"], 0.0)
        self.assertGreater(result["r2"], 0.9)

    async def test_layout_rejects_out_of_range_pi_location(self):
        response = await main.api_layout(_FakeRequest({"width": 8, "height": 8, "pi_x": 99, "pi_y": 1}))
        self.assertEqual(response.status, 400)
        payload = json.loads(response.text)
        self.assertIn("pi location", payload["error"])

    async def test_capture_requires_active_session(self):
        response = await main.api_capture(_FakeRequest({"beacon_mac": "AA:BB", "x": 1, "y": 1}))
        self.assertEqual(response.status, 400)
        payload = json.loads(response.text)
        self.assertIn("start a session", payload["error"])


if __name__ == "__main__":
    unittest.main()
