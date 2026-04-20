import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
spec = importlib.util.spec_from_file_location("ble_sensor_main", MODULE_PATH)
ble_sensor_main = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(ble_sensor_main)


class BleLocalQueueTests(unittest.TestCase):
    def test_queue_and_read_pending_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "ble-test.db")
            ble_sensor_main._init_local_store(db_path)

            payload = {
                "vehicle_id": "truck-1",
                "captured_at_utc": "2026-04-20T00:00:00+00:00",
                "event_type": "ble_sensor_scan",
                "sensor_count": 0,
                "scan_duration_seconds": 20,
                "sensors": [],
            }
            ble_sensor_main._queue_scan_payload(db_path, payload)
            rows = ble_sensor_main._get_pending_scan_payloads(db_path, limit=10)

            self.assertEqual(len(rows), 1)
            _, _, payload_json = rows[0]
            stored_payload = json.loads(payload_json)
            self.assertEqual(stored_payload["captured_at_utc"], payload["captured_at_utc"])
            self.assertEqual(stored_payload["sensor_count"], 0)


class BleFlushTests(unittest.IsolatedAsyncioTestCase):
    async def test_flush_marks_payload_sent_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "ble-test.db")
            ble_sensor_main._init_local_store(db_path)
            ble_sensor_main._queue_scan_payload(
                db_path,
                {
                    "vehicle_id": "truck-1",
                    "captured_at_utc": "2026-04-20T00:00:00+00:00",
                    "event_type": "ble_sensor_scan",
                    "sensor_count": 1,
                    "scan_duration_seconds": 20,
                    "sensors": [{"device_id": "abc"}],
                },
            )

            async def _always_ok(_session, _payload, _config):
                return True

            original_push = ble_sensor_main.push_to_cloud
            ble_sensor_main.push_to_cloud = _always_ok
            try:
                config = ble_sensor_main.Config(
                    webhook_url="https://example.test/ingest",
                    vehicle_id="truck-1",
                    post_interval_seconds=60,
                    scan_duration_seconds=20,
                    api_key="",
                    request_timeout_seconds=10,
                    anonymize_mac=True,
                    mac_hash_salt="salt",
                    include_name=False,
                    max_devices_per_scan=0,
                    key_beacon_uuids=frozenset(),
                    key_beacon_manufacturer_ids=frozenset(),
                    scan_contention_backoff_cap_seconds=12,
                    scan_contention_cooldown_threshold=3,
                    local_db_path=db_path,
                    upload_batch_size=25,
                )
                await ble_sensor_main._flush_pending_scan_payloads(None, config)
            finally:
                ble_sensor_main.push_to_cloud = original_push

            self.assertEqual(
                len(ble_sensor_main._get_pending_scan_payloads(db_path, limit=10)),
                0,
            )


if __name__ == "__main__":
    unittest.main()
