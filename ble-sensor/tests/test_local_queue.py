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
            _, _, payload_json, _ = rows[0]
            stored_payload = json.loads(payload_json)
            self.assertEqual(stored_payload["captured_at_utc"], payload["captured_at_utc"])
            self.assertEqual(stored_payload["sensor_count"], 0)


class MacNormalizationTests(unittest.TestCase):
    def test_canonicalize_mac_enforces_uppercase_colon_format(self) -> None:
        self.assertEqual(
            ble_sensor_main._canonicalize_mac_address("e45f01aabbcc"),
            "E4:5F:01:AA:BB:CC",
        )
        self.assertEqual(
            ble_sensor_main._canonicalize_mac_address("e4-5f-01-aa-bb-cc"),
            "E4:5F:01:AA:BB:CC",
        )

    def test_normalize_mac_hash_uses_canonicalized_input(self) -> None:
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
            scan_contention_max_attempts_before_reset=10,
            local_db_path=":memory:",
            upload_batch_size=25,
            telematics_db_path="/data/telematics.db",
            pi_location_db_path="/data/telematics.db",
            pi_location_cache_path="/data/telematics_last_locked.json",
            pi_location_query_timeout_seconds=0.25,
            pi_location_stale_after_seconds=180,
            pi_id="pi-a",
            tracked_asset_registry_path="/tmp/tracked-assets.json",
            resolver_candidate_window_seconds=60,
            resolver_tie_epsilon=0.02,
            resolver_stationary_mode_enabled=False,
            upload_batch_max_bytes=200000,
            upload_backoff_initial_seconds=5,
            upload_backoff_max_seconds=300,
        )
        self.assertEqual(
            ble_sensor_main._normalize_mac("e45f01aabbcc", config),
            ble_sensor_main._normalize_mac("E4:5F:01:AA:BB:CC", config),
        )


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
                    scan_contention_max_attempts_before_reset=10,
                    local_db_path=db_path,
                    upload_batch_size=25,
                    telematics_db_path="/data/telematics.db",
                    pi_location_db_path="/data/telematics.db",
                    pi_location_cache_path="/data/telematics_last_locked.json",
                    pi_location_query_timeout_seconds=0.25,
                    pi_location_stale_after_seconds=180,
                    pi_id="pi-a",
                    tracked_asset_registry_path="/tmp/tracked-assets.json",
                    resolver_candidate_window_seconds=60,
                    resolver_tie_epsilon=0.02,
                    resolver_stationary_mode_enabled=False,
                    upload_batch_max_bytes=200000,
                    upload_backoff_initial_seconds=5,
                    upload_backoff_max_seconds=300,
                )
                await ble_sensor_main._flush_pending_scan_payloads(None, config)
            finally:
                ble_sensor_main.push_to_cloud = original_push

            self.assertEqual(
                len(ble_sensor_main._get_pending_scan_payloads(db_path, limit=10)),
                0,
            )

    async def test_flush_batch_partial_ack_marks_only_successful_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "ble-test.db")
            ble_sensor_main._init_local_store(db_path)
            for second in ("00", "01"):
                ble_sensor_main._queue_scan_payload(
                    db_path,
                    {
                        "vehicle_id": "truck-1",
                        "captured_at_utc": f"2026-04-20T00:00:{second}+00:00",
                        "event_type": "ble_sensor_scan",
                        "sensor_count": 1,
                        "scan_duration_seconds": 20,
                        "sensors": [{"device_id": f"abc-{second}"}],
                        "pi_location": {"latitude": None, "longitude": None},
                    },
                )

            async def _batch_partial(_session, _payload, _config):
                return True, {2}

            original_batch = ble_sensor_main._post_scan_batch
            ble_sensor_main._post_scan_batch = _batch_partial
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
                    scan_contention_max_attempts_before_reset=10,
                    local_db_path=db_path,
                    upload_batch_size=25,
                    telematics_db_path="/data/telematics.db",
                    pi_location_db_path="/data/telematics.db",
                    pi_location_cache_path="/data/telematics_last_locked.json",
                    pi_location_query_timeout_seconds=0.25,
                    pi_location_stale_after_seconds=180,
                    pi_id="pi-a",
                    tracked_asset_registry_path="/tmp/tracked-assets.json",
                    resolver_candidate_window_seconds=60,
                    resolver_tie_epsilon=0.02,
                    resolver_stationary_mode_enabled=False,
                    upload_batch_max_bytes=200000,
                    upload_backoff_initial_seconds=1,
                    upload_backoff_max_seconds=8,
                )
                await ble_sensor_main._flush_pending_scan_payloads(None, config)
            finally:
                ble_sensor_main._post_scan_batch = original_batch

            with sqlite3.connect(db_path) as conn:
                pending = conn.execute(
                    "SELECT id FROM ble_scan_events WHERE sent_at_utc IS NULL ORDER BY id ASC"
                ).fetchall()
            self.assertEqual(len(pending), 1)
            self.assertEqual(int(pending[0][0]), 2)

    async def test_flush_batch_failure_falls_back_to_single_uploads(self) -> None:
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
                    "pi_location": {"latitude": None, "longitude": None},
                },
            )

            async def _batch_fail(_session, _payload, _config):
                return False, None

            async def _single_ok(_session, _payload, _config):
                return True

            original_batch = ble_sensor_main._post_scan_batch
            original_single = ble_sensor_main.push_to_cloud
            ble_sensor_main._post_scan_batch = _batch_fail
            ble_sensor_main.push_to_cloud = _single_ok
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
                    scan_contention_max_attempts_before_reset=10,
                    local_db_path=db_path,
                    upload_batch_size=25,
                    telematics_db_path="/data/telematics.db",
                    pi_location_db_path="/data/telematics.db",
                    pi_location_cache_path="/data/telematics_last_locked.json",
                    pi_location_query_timeout_seconds=0.25,
                    pi_location_stale_after_seconds=180,
                    pi_id="pi-a",
                    tracked_asset_registry_path="/tmp/tracked-assets.json",
                    resolver_candidate_window_seconds=60,
                    resolver_tie_epsilon=0.02,
                    resolver_stationary_mode_enabled=False,
                    upload_batch_max_bytes=200000,
                    upload_backoff_initial_seconds=5,
                    upload_backoff_max_seconds=300,
                )
                await ble_sensor_main._flush_pending_scan_payloads(None, config)
            finally:
                ble_sensor_main._post_scan_batch = original_batch
                ble_sensor_main.push_to_cloud = original_single

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
                scan_contention_max_attempts_before_reset=10,
                local_db_path=str(Path(tmpdir) / "ble.db"),
                upload_batch_size=25,
                telematics_db_path=telematics_db_path,
                pi_location_db_path=telematics_db_path,
                pi_location_cache_path=str(Path(tmpdir) / "missing.json"),
                pi_location_query_timeout_seconds=0.25,
                pi_location_stale_after_seconds=99999999,
                pi_id="pi-a",
                tracked_asset_registry_path=str(Path(tmpdir) / "tracked-assets.json"),
                resolver_candidate_window_seconds=60,
                resolver_tie_epsilon=0.02,
                resolver_stationary_mode_enabled=False,
            upload_batch_max_bytes=200000,
            upload_backoff_initial_seconds=5,
            upload_backoff_max_seconds=300,
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
                scan_contention_max_attempts_before_reset=10,
                local_db_path=str(Path(tmpdir) / "ble.db"),
                upload_batch_size=25,
                telematics_db_path=str(Path(tmpdir) / "missing.db"),
                pi_location_db_path=str(Path(tmpdir) / "missing.db"),
                pi_location_cache_path=str(Path(tmpdir) / "missing.json"),
                pi_location_query_timeout_seconds=0.25,
                pi_location_stale_after_seconds=180,
                pi_id="pi-a",
                tracked_asset_registry_path=str(Path(tmpdir) / "tracked-assets.json"),
                resolver_candidate_window_seconds=60,
                resolver_tie_epsilon=0.02,
                resolver_stationary_mode_enabled=False,
            upload_batch_max_bytes=200000,
            upload_backoff_initial_seconds=5,
            upload_backoff_max_seconds=300,
            )

            pi_location = await ble_sensor_main._collect_pi_location(config)
            self.assertEqual(pi_location["fix_status"], "stale_or_unknown")
            self.assertIsNone(pi_location["latitude"])
            self.assertIsNone(pi_location["longitude"])


class TrackedAssetResolverTests(unittest.TestCase):
    def test_resolver_applies_centroid_on_tie_within_epsilon(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "ble.db")
            registry_path = Path(tmpdir) / "tracked-assets.json"
            registry_path.write_text(
                json.dumps(
                    [
                        {
                            "ble_mac_normalized": "AA:BB:CC:DD:EE:FF",
                            "label": "Pallet-1",
                            "active": True,
                        }
                    ]
                ),
                encoding="utf-8",
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
                scan_contention_max_attempts_before_reset=10,
                local_db_path=db_path,
                upload_batch_size=25,
                telematics_db_path=str(Path(tmpdir) / "telematics.db"),
                pi_location_db_path=str(Path(tmpdir) / "telematics.db"),
                pi_location_cache_path=str(Path(tmpdir) / "telematics_last_locked.json"),
                pi_location_query_timeout_seconds=0.25,
                pi_location_stale_after_seconds=180,
                pi_id="pi-a",
                tracked_asset_registry_path=str(registry_path),
                resolver_candidate_window_seconds=60,
                resolver_tie_epsilon=0.05,
                resolver_stationary_mode_enabled=False,
            upload_batch_max_bytes=200000,
            upload_backoff_initial_seconds=5,
            upload_backoff_max_seconds=300,
            )
            ble_sensor_main._init_local_store(db_path)
            ble_sensor_main._sync_tracked_asset_registry(config)

            ble_sensor_main._record_scan_observations(
                db_path,
                {
                    "vehicle_id": "truck-1",
                    "pi_id": "pi-a",
                    "captured_at_utc": "2026-04-20T00:00:30+00:00",
                    "sensors": [{"mac_address": "AA:BB:CC:DD:EE:FF", "rssi": -55}],
                    "pi_location": {
                        "latitude": 35.0,
                        "longitude": -84.0,
                        "gps_timestamp": "2026-04-20T00:00:25+00:00",
                        "location_age_sec": 5,
                    },
                },
            )
            ble_sensor_main._record_scan_observations(
                db_path,
                {
                    "vehicle_id": "truck-1",
                    "pi_id": "pi-b",
                    "captured_at_utc": "2026-04-20T00:00:31+00:00",
                    "sensors": [{"mac_address": "AA:BB:CC:DD:EE:FF", "rssi": -56}],
                    "pi_location": {
                        "latitude": 37.0,
                        "longitude": -82.0,
                        "gps_timestamp": "2026-04-20T00:00:26+00:00",
                        "location_age_sec": 5,
                    },
                },
            )

            ble_sensor_main._resolve_tracked_asset_positions(config)
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT resolution_method, candidate_count, confidence_score, resolved_latitude, resolved_longitude
                    FROM tracked_asset_resolutions
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
            assert row is not None
            self.assertEqual(row[0], "centroid_top2")
            self.assertEqual(row[1], 2)
            self.assertGreaterEqual(float(row[2]), 0.0)
            self.assertAlmostEqual(float(row[3]), 36.0, places=3)
            self.assertAlmostEqual(float(row[4]), -83.0, places=3)

    def test_resolver_lexicographic_tiebreak_without_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "ble.db")
            registry_path = Path(tmpdir) / "tracked-assets.json"
            registry_path.write_text(
                json.dumps([{"ble_mac_normalized": "AA:BB:CC:DD:EE:FF", "active": True}]),
                encoding="utf-8",
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
                scan_contention_max_attempts_before_reset=10,
                local_db_path=db_path,
                upload_batch_size=25,
                telematics_db_path=str(Path(tmpdir) / "telematics.db"),
                pi_location_db_path=str(Path(tmpdir) / "telematics.db"),
                pi_location_cache_path=str(Path(tmpdir) / "telematics_last_locked.json"),
                pi_location_query_timeout_seconds=0.25,
                pi_location_stale_after_seconds=180,
                pi_id="pi-a",
                tracked_asset_registry_path=str(registry_path),
                resolver_candidate_window_seconds=60,
                resolver_tie_epsilon=0.2,
                resolver_stationary_mode_enabled=False,
                upload_batch_max_bytes=200000,
                upload_backoff_initial_seconds=5,
                upload_backoff_max_seconds=300,
            )
            ble_sensor_main._init_local_store(db_path)
            ble_sensor_main._sync_tracked_asset_registry(config)
            for pi_id in ("pi-z", "pi-a"):
                ble_sensor_main._record_scan_observations(
                    db_path,
                    {
                        "vehicle_id": "truck-1",
                        "pi_id": pi_id,
                        "captured_at_utc": "2026-04-20T00:00:30+00:00",
                        "sensors": [{"mac_address": "AA:BB:CC:DD:EE:FF", "rssi": -70}],
                        "pi_location": {"latitude": None, "longitude": None, "location_age_sec": 50},
                    },
                )
            ble_sensor_main._resolve_tracked_asset_positions(config)

            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT resolution_method, resolved_pi_id
                    FROM tracked_asset_resolutions
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
            assert row is not None
            self.assertEqual(row[0], "lexicographic_pi_tiebreak")
            self.assertEqual(row[1], "pi-a")


class BleScanContentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_contention_path_does_not_raise_attribute_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "ble-test.db")
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
                scan_contention_max_attempts_before_reset=2,
                local_db_path=db_path,
                upload_batch_size=25,
                telematics_db_path="/data/telematics.db",
                pi_location_db_path="/data/telematics.db",
                pi_location_cache_path="/data/telematics_last_locked.json",
                pi_location_query_timeout_seconds=0.25,
                pi_location_stale_after_seconds=180,
                pi_id="pi-a",
                tracked_asset_registry_path="/tmp/tracked-assets.json",
                resolver_candidate_window_seconds=60,
                resolver_tie_epsilon=0.02,
                resolver_stationary_mode_enabled=False,
                upload_batch_max_bytes=200000,
                upload_backoff_initial_seconds=5,
                upload_backoff_max_seconds=300,
            )

            async def _contention(*_args, **_kwargs):
                raise RuntimeError("org.bluez.Error.InProgress")

            class _FakeClientSession:
                async def __aenter__(self):
                    return object()

                async def __aexit__(self, exc_type, exc, tb):
                    return False

            async def _interrupting_sleep(_seconds):
                raise KeyboardInterrupt()

            original_load_config = ble_sensor_main.load_config
            original_collect_payload = ble_sensor_main.collect_payload
            original_client_session = ble_sensor_main.aiohttp.ClientSession
            original_sleep = ble_sensor_main.asyncio.sleep
            original_uniform = ble_sensor_main.random.uniform
            try:
                ble_sensor_main.load_config = lambda: config
                ble_sensor_main.collect_payload = _contention
                ble_sensor_main.aiohttp.ClientSession = lambda: _FakeClientSession()
                ble_sensor_main.asyncio.sleep = _interrupting_sleep
                ble_sensor_main.random.uniform = lambda _a, _b: 0.0

                with self.assertRaises(KeyboardInterrupt):
                    await ble_sensor_main.run()
            finally:
                ble_sensor_main.load_config = original_load_config
                ble_sensor_main.collect_payload = original_collect_payload
                ble_sensor_main.aiohttp.ClientSession = original_client_session
                ble_sensor_main.asyncio.sleep = original_sleep
                ble_sensor_main.random.uniform = original_uniform


class PiGpsInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_pi_gps_from_telematics_db_returns_contract_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            telematics_db_path = str(Path(tmpdir) / "telematics.db")
            with sqlite3.connect(telematics_db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE gps_points (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lat REAL,
                        lon REAL,
                        fix_status TEXT,
                        captured_at_utc TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO gps_points (lat, lon, fix_status, captured_at_utc)
                    VALUES (?, ?, ?, ?)
                    """,
                    (33.7490, -84.3880, "locked", "2026-04-21T00:00:00+00:00"),
                )

            pi_gps = ble_sensor_main._read_pi_gps_from_telematics_db(
                telematics_db_path,
                lock_timeout_seconds=0.2,
            )
            assert pi_gps is not None
            self.assertEqual(
                set(pi_gps.keys()),
                {"latitude", "longitude", "speed_kmh", "fix_status", "captured_at_utc"},
            )
            self.assertEqual(pi_gps["fix_status"], "locked")

    async def test_collect_payload_includes_pi_gps_when_lock_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            telematics_db_path = str(Path(tmpdir) / "telematics.db")
            with sqlite3.connect(telematics_db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE gps_points (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lat REAL,
                        lon REAL,
                        fix_status TEXT,
                        captured_at_utc TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO gps_points (lat, lon, fix_status, captured_at_utc)
                    VALUES (?, ?, ?, ?)
                    """,
                    (33.9000, -84.2000, "locked", "2026-04-21T00:10:00+00:00"),
                )

            config = ble_sensor_main.Config(
                webhook_url="https://example.test/ingest",
                vehicle_id="truck-1",
                post_interval_seconds=60,
                scan_duration_seconds=1,
                api_key="",
                request_timeout_seconds=10,
                anonymize_mac=False,
                mac_hash_salt="salt",
                include_name=False,
                max_devices_per_scan=0,
                key_beacon_uuids=frozenset(),
                key_beacon_manufacturer_ids=frozenset(),
                scan_contention_backoff_cap_seconds=12,
                scan_contention_cooldown_threshold=3,
                scan_contention_max_attempts_before_reset=2,
                local_db_path=str(Path(tmpdir) / "ble.db"),
                upload_batch_size=25,
                telematics_db_path=telematics_db_path,
                pi_location_db_path=telematics_db_path,
                pi_location_cache_path=str(Path(tmpdir) / "missing.json"),
                pi_location_query_timeout_seconds=0.25,
                pi_location_stale_after_seconds=180,
                pi_id="pi-a",
                tracked_asset_registry_path=str(Path(tmpdir) / "tracked-assets.json"),
                resolver_candidate_window_seconds=60,
                resolver_tie_epsilon=0.02,
                resolver_stationary_mode_enabled=False,
                upload_batch_max_bytes=200000,
                upload_backoff_initial_seconds=5,
                upload_backoff_max_seconds=300,
            )

            original_discover = ble_sensor_main.BleakScanner.discover
            original_collect_pi_location = ble_sensor_main._collect_pi_location
            try:
                async def _no_devices(**_kwargs):
                    return {}

                async def _stub_pi_location(_config):
                    return {"latitude": None, "longitude": None, "fix_status": "stale_or_unknown"}

                ble_sensor_main.BleakScanner.discover = _no_devices
                ble_sensor_main._collect_pi_location = _stub_pi_location

                payload = await ble_sensor_main.collect_payload(config)
            finally:
                ble_sensor_main.BleakScanner.discover = original_discover
                ble_sensor_main._collect_pi_location = original_collect_pi_location

            self.assertIn("pi_gps", payload)
            self.assertEqual(payload["pi_gps"]["latitude"], 33.9)
            self.assertEqual(payload["pi_gps"]["fix_status"], "locked")


if __name__ == "__main__":
    unittest.main()
