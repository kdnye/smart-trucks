import asyncio
import importlib.util
import json
import sqlite3
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
                "pi_location": {
                    "latitude": None,
                    "longitude": None,
                    "fix_status": "stale_or_unknown",
                    "gps_timestamp": None,
                    "source": "telematics.db:last_locked",
                    "location_age_sec": None,
                },
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
                    "pi_location": {
                        "latitude": None,
                        "longitude": None,
                        "fix_status": "stale_or_unknown",
                        "gps_timestamp": None,
                        "source": "telematics.db:last_locked",
                        "location_age_sec": None,
                    },
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
                    pi_location_db_path="/data/telematics.db",
                    pi_location_cache_path="/data/telematics_last_locked.json",
                    pi_location_query_timeout_seconds=0.25,
                    pi_location_stale_after_seconds=180,
                )
                await ble_sensor_main._flush_pending_scan_payloads(None, config)
            finally:
                ble_sensor_main.push_to_cloud = original_push

            self.assertEqual(
                len(ble_sensor_main._get_pending_scan_payloads(db_path, limit=10)),
                0,
            )


class PiLocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_fresh_pi_location_from_telematics_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            telematics_db_path = str(Path(tmpdir) / "telematics.db")
            with sqlite3.connect(telematics_db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE gps_points (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lat REAL,
                        lon REAL,
                        fix_status TEXT NOT NULL,
                        captured_at_utc TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO gps_points(lat, lon, fix_status, captured_at_utc, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        33.7490,
                        -84.3880,
                        "locked",
                        "2026-04-20T00:00:00+00:00",
                        json.dumps(
                            {
                                "location": {
                                    "gps_timestamp": "2026-04-20T00:00:00+00:00",
                                }
                            }
                        ),
                    ),
                )

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
                local_db_path=str(Path(tmpdir) / "ble.db"),
                upload_batch_size=25,
                pi_location_db_path=telematics_db_path,
                pi_location_cache_path=str(Path(tmpdir) / "missing.json"),
                pi_location_query_timeout_seconds=0.25,
                pi_location_stale_after_seconds=99999999,
            )

            pi_location = await ble_sensor_main._collect_pi_location(config)
            self.assertEqual(pi_location["fix_status"], "locked")
            self.assertEqual(pi_location["latitude"], 33.7490)
            self.assertEqual(pi_location["longitude"], -84.3880)
            self.assertEqual(pi_location["source"], "telematics.db:last_locked")

    async def test_returns_stale_status_when_no_last_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
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
                local_db_path=str(Path(tmpdir) / "ble.db"),
                upload_batch_size=25,
                pi_location_db_path=str(Path(tmpdir) / "missing.db"),
                pi_location_cache_path=str(Path(tmpdir) / "missing.json"),
                pi_location_query_timeout_seconds=0.25,
                pi_location_stale_after_seconds=180,
            )

            pi_location = await ble_sensor_main._collect_pi_location(config)
            self.assertEqual(pi_location["fix_status"], "stale_or_unknown")
            self.assertIsNone(pi_location["latitude"])
            self.assertIsNone(pi_location["longitude"])


if __name__ == "__main__":
    unittest.main()
