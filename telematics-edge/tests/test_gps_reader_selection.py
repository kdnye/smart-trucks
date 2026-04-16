import asyncio
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


class FakeNMEAReader:
    created_ports: list[str] = []

    def __init__(self, port: str, baudrate: int) -> None:
        self.port = port
        self.baudrate = baudrate
        FakeNMEAReader.created_ports.append(port)

    async def read_loop(self, _on_reading):
        raise asyncio.CancelledError


class GpsReaderWorkerSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeNMEAReader.created_ports.clear()

    def _build_config(self, candidates: tuple[str, ...], probe_all: bool) -> main.Config:
        return main.Config(
            vehicle_id="TRUCK",
            webhook_url=None,
            api_key="",
            gps_sample_interval_seconds=5,
            heartbeat_interval_seconds=60,
            sync_interval_seconds=20,
            gps_serial_candidates=candidates,
            gps_probe_all_candidates=probe_all,
            gps_baud_rate=9600,
            sync_batch_size=50,
            sync_backoff_max_seconds=300,
            queue_alert_depth=1000,
            power_snapshot_max_age_seconds=30,
            imu_i2c_bus=1,
            imu_expected_addresses=(0x6A,),
            imu_required=True,
        )

    async def test_tcp_primary_remains_selected_when_probe_enabled(self) -> None:
        config = self._build_config(("tcp://127.0.0.1:10110", "/dev/ttyS0"), probe_all=True)
        state = main.RuntimeState(start_monotonic=0.0)

        with (
            patch.object(main, "NMEAReader", FakeNMEAReader),
            patch.object(main.os.path, "exists", side_effect=AssertionError("exists should not be called for TCP primary")),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await main.gps_reader_worker(config, state)

        self.assertEqual(FakeNMEAReader.created_ports, ["tcp://127.0.0.1:10110"])

    async def test_serial_primary_still_probes_candidates_when_enabled(self) -> None:
        config = self._build_config(("/dev/serial0", "/dev/ttyS0"), probe_all=True)
        state = main.RuntimeState(start_monotonic=0.0)

        def fake_exists(path: str) -> bool:
            return path == "/dev/ttyS0"

        with (
            patch.object(main, "NMEAReader", FakeNMEAReader),
            patch.object(main.os.path, "exists", side_effect=fake_exists),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await main.gps_reader_worker(config, state)

        self.assertEqual(FakeNMEAReader.created_ports, ["/dev/ttyS0"])


if __name__ == "__main__":
    unittest.main()
