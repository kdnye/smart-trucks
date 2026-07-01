"""Unit tests for the ATOM Lite anchor ingest path in ble-sensor.

Covers: frame handling with injected fakes (no broker/hardware), registry
quarantine, payload shapes vs EDGE_DASHBOARD_CONTRACT.md, the observer_id
schema migration in _init_local_store, and resolver v2's observer ranking.
"""

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

BLE_SENSOR_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BLE_SENSOR_DIR.parent
sys.path.insert(0, str(BLE_SENSOR_DIR))
sys.path.insert(0, str(REPO_ROOT))

MAIN_SPEC = importlib.util.spec_from_file_location(
    "ble_sensor_main", BLE_SENSOR_DIR / "main.py"
)
main = importlib.util.module_from_spec(MAIN_SPEC)
assert MAIN_SPEC and MAIN_SPEC.loader
MAIN_SPEC.loader.exec_module(main)

import anchor_ingest  # noqa: E402  (resolved via BLE_SENSOR_DIR on sys.path)


def _scan_frame(**overrides):
    frame = {
        "type": "anchor_scan",
        "anchor_id": "anchor-a1b2c3",
        "gateway_id": "anchor-ffeedd",
        "seq": 42,
        "part": 0,
        "part_count": 1,
        "window_s": 15,
        "link_rssi": -58,
        "devices": [
            {"mac": "A4:C1:38:AA:BB:CC", "rssi": -67, "max_rssi": -60, "count": 5},
            {"mac": "d413390a0b0c", "rssi": -75, "max_rssi": -70, "count": 2},
        ],
        "bridge_received_at_utc": "2026-07-01T12:00:00+00:00",
    }
    frame.update(overrides)
    return frame


def _heartbeat_frame(**overrides):
    frame = {
        "type": "anchor_heartbeat",
        "anchor_id": "anchor-a1b2c3",
        "gateway_id": "anchor-ffeedd",
        "seq": 7,
        "uptime_s": 3600,
        "free_heap": 123456,
        "fw": "1.0.0",
        "link_rssi": -58,
        "report_interval_s": 15,
        "rssi_floor": -90,
        "channel": 1,
        "bridge_received_at_utc": "2026-07-01T12:00:00+00:00",
    }
    frame.update(overrides)
    return frame


class _Recorder:
    def __init__(self):
        self.observations = []
        self.enqueued = []

    def record(self, payload):
        self.observations.append(payload)

    def enqueue(self, payload):
        self.enqueued.append(payload)


def _make_ctx(tmpdir, recorder, *, allowlist=frozenset(), registry_path=""):
    db_path = str(Path(tmpdir) / "ble-sensor.db")
    main._init_local_store(db_path)
    config = anchor_ingest.IngestConfig(
        enabled=True,
        mqtt_host="127.0.0.1",
        mqtt_port=1883,
        registry_path=registry_path,
        allowlist=allowlist,
        registry_refresh_seconds=10,
    )
    return anchor_ingest.IngestContext(
        config=config,
        pi_id="pi-test-01",
        vehicle_id="TRK-905",
        local_db_path=db_path,
        record_observations=recorder.record,
        enqueue_scan=recorder.enqueue,
        get_pi_location=lambda: {
            "latitude": 45.0,
            "longitude": -122.0,
            "fix_status": "locked",
            "gps_timestamp": "2026-07-01T11:59:30+00:00",
            "source": "test",
            "location_age_sec": 30,
        },
        classify_mac=lambda mac: "Govee" if mac.startswith("A4:C1:38") else "Unknown",
        canonicalize_mac=main._canonicalize_mac_address,
    )


class AnchorScanIngestTests(unittest.TestCase):
    def test_scan_records_observations_and_enqueues_cloud_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = _Recorder()
            ctx = _make_ctx(tmp, recorder)  # open mode
            anchor_ingest.handle_frame(ctx, "anchors/anchor-a1b2c3/scan", _scan_frame())

            self.assertEqual(len(recorder.observations), 1)
            self.assertEqual(len(recorder.enqueued), 1)
            payload = recorder.enqueued[0]

            # Contract fields sync-service/cloud rely on.
            self.assertEqual(payload["event_type"], "ble_sensor_scan")
            self.assertEqual(payload["observer_id"], "anchor-a1b2c3")
            self.assertEqual(payload["parent_pi_id"], "pi-test-01")
            self.assertEqual(payload["pi_id"], "pi-test-01")
            self.assertEqual(payload["vehicle_id"], "TRK-905")
            self.assertEqual(payload["sensor_count"], 2)
            self.assertIn("idempotency_key", payload)
            self.assertIn("anchor-a1b2c3", payload["idempotency_key"])
            self.assertEqual(payload["anchor"]["link_rssi"], -58)
            self.assertEqual(payload["pi_location"]["latitude"], 45.0)

            # MACs are canonicalized; median rssi flows into the sensor rssi.
            sensors = payload["sensors"]
            self.assertEqual(sensors[0]["mac_address"], "A4:C1:38:AA:BB:CC")
            self.assertEqual(sensors[0]["device_type"], "Govee")
            self.assertEqual(sensors[1]["mac_address"], "D4:13:39:0A:0B:0C")
            self.assertEqual(sensors[0]["rssi"], -67)
            self.assertEqual(sensors[0]["metadata"]["source"], "ble_anchor")

    def test_scan_observations_land_with_observer_id(self):
        """End-to-end into SQLite via main's real _record_scan_observations."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "ble-sensor.db")
            main._init_local_store(db_path)
            recorder = _Recorder()
            ctx = _make_ctx(tmp, recorder)
            ctx = anchor_ingest.IngestContext(
                **{
                    **ctx.__dict__,
                    "local_db_path": db_path,
                    "record_observations": lambda payload: main._record_scan_observations(
                        db_path, payload
                    ),
                }
            )
            anchor_ingest.handle_frame(ctx, "anchors/anchor-a1b2c3/scan", _scan_frame())

            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT observer_id, tracked_mac_normalized, rssi FROM ble_device_observations"
                ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(r[0] == "anchor-a1b2c3" for r in rows))

    def test_scan_updates_anchor_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = _Recorder()
            ctx = _make_ctx(tmp, recorder)
            anchor_ingest.handle_frame(ctx, "anchors/anchor-a1b2c3/scan", _scan_frame())
            with sqlite3.connect(ctx.local_db_path) as conn:
                row = conn.execute(
                    "SELECT anchor_id, parent_pi_id, role, registered, link_rssi FROM anchor_status"
                ).fetchone()
            self.assertEqual(row[0], "anchor-a1b2c3")
            self.assertEqual(row[1], "pi-test-01")
            self.assertEqual(row[2], "anchor")
            self.assertEqual(row[3], 1)  # open mode => registered
            self.assertEqual(row[4], -58)

    def test_heartbeat_enqueues_zero_sensor_payload_and_updates_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = _Recorder()
            ctx = _make_ctx(tmp, recorder)
            anchor_ingest.handle_frame(
                ctx, "anchors/anchor-a1b2c3/status", _heartbeat_frame()
            )
            self.assertEqual(len(recorder.enqueued), 1)
            payload = recorder.enqueued[0]
            self.assertEqual(payload["sensor_count"], 0)
            self.assertEqual(payload["anchor"]["fw"], "1.0.0")
            self.assertEqual(payload["anchor"]["uptime_s"], 3600)
            with sqlite3.connect(ctx.local_db_path) as conn:
                row = conn.execute(
                    "SELECT fw_version, free_heap, uptime_s FROM anchor_status"
                ).fetchone()
            self.assertEqual(row, ("1.0.0", 123456, 3600))

    def test_gateway_status_tracked_with_gateway_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = _Recorder()
            ctx = _make_ctx(tmp, recorder)
            anchor_ingest.handle_frame(
                ctx,
                "anchors/anchor-ffeedd/gateway",
                {"type": "gateway_status", "gateway_id": "anchor-ffeedd", "fw": "1.0.0"},
            )
            self.assertEqual(recorder.enqueued, [])
            with sqlite3.connect(ctx.local_db_path) as conn:
                row = conn.execute("SELECT anchor_id, role FROM anchor_status").fetchone()
            self.assertEqual(row, ("anchor-ffeedd", "gateway"))

    def test_bridge_status_topic_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = _Recorder()
            ctx = _make_ctx(tmp, recorder)
            anchor_ingest.handle_frame(
                ctx, "anchors/bridge/status", {"online": True, "serial_port": "/dev/ttyACM0"}
            )
            self.assertEqual(recorder.enqueued, [])
            self.assertEqual(recorder.observations, [])

    def test_invalid_anchor_id_in_topic_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = _Recorder()
            ctx = _make_ctx(tmp, recorder)
            anchor_ingest.handle_frame(ctx, "anchors/evil-node/scan", _scan_frame())
            self.assertEqual(recorder.enqueued, [])


class QuarantineTests(unittest.TestCase):
    def test_unlisted_anchor_is_status_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = _Recorder()
            ctx = _make_ctx(tmp, recorder, allowlist=frozenset({"anchor-ffeedd"}))
            anchor_ingest.handle_frame(ctx, "anchors/anchor-a1b2c3/scan", _scan_frame())

            self.assertEqual(recorder.observations, [])
            self.assertEqual(recorder.enqueued, [])
            with sqlite3.connect(ctx.local_db_path) as conn:
                row = conn.execute(
                    "SELECT anchor_id, registered FROM anchor_status"
                ).fetchone()
            self.assertEqual(row, ("anchor-a1b2c3", 0))

    def test_allowlisted_anchor_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = _Recorder()
            ctx = _make_ctx(tmp, recorder, allowlist=frozenset({"anchor-a1b2c3"}))
            anchor_ingest.handle_frame(ctx, "anchors/anchor-a1b2c3/scan", _scan_frame())
            self.assertEqual(len(recorder.enqueued), 1)

    def test_registry_file_inactive_anchor_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = str(Path(tmp) / "anchor_registry.json")
            with open(registry_path, "w", encoding="utf-8") as f:
                json.dump(
                    [
                        {"anchor_id": "anchor-a1b2c3", "active": False},
                        {"anchor_id": "anchor-ffeedd", "active": True},
                    ],
                    f,
                )
            recorder = _Recorder()
            ctx = _make_ctx(tmp, recorder, registry_path=registry_path)
            anchor_ingest.handle_frame(ctx, "anchors/anchor-a1b2c3/scan", _scan_frame())
            self.assertEqual(recorder.enqueued, [])
            anchor_ingest.handle_frame(
                ctx, "anchors/anchor-ffeedd/scan", _scan_frame(anchor_id="anchor-ffeedd")
            )
            self.assertEqual(len(recorder.enqueued), 1)


class RegistryLoaderTests(unittest.TestCase):
    def test_nothing_configured_means_open_mode(self):
        self.assertIsNone(anchor_ingest.load_anchor_registry("", frozenset()))
        self.assertIsNone(
            anchor_ingest.load_anchor_registry("/nonexistent/path.json", frozenset())
        )

    def test_allowlist_only(self):
        registry = anchor_ingest.load_anchor_registry("", frozenset({"anchor-a1b2c3"}))
        self.assertEqual(registry, {"anchor-a1b2c3": True})

    def test_corrupt_registry_file_with_allowlist_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "broken.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            registry = anchor_ingest.load_anchor_registry(path, frozenset({"anchor-a1b2c3"}))
            self.assertEqual(registry, {"anchor-a1b2c3": True})

    def test_empty_registry_file_fails_closed_not_open(self):
        """A configured-but-empty registry means 'no anchors approved' — it
        must NOT fall back to open mode (allowlist bypass)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "empty.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("[]")
            registry = anchor_ingest.load_anchor_registry(path, frozenset())
            self.assertEqual(registry, {})  # closed: quarantine everything

    def test_corrupt_registry_file_without_allowlist_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "broken.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            registry = anchor_ingest.load_anchor_registry(path, frozenset())
            self.assertEqual(registry, {})  # fail closed, not open

    def test_empty_registry_file_quarantines_scans(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "empty.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("[]")
            recorder = _Recorder()
            ctx = _make_ctx(tmp, recorder, registry_path=path)
            anchor_ingest.handle_frame(ctx, "anchors/anchor-a1b2c3/scan", _scan_frame())
            self.assertEqual(recorder.enqueued, [])
            self.assertEqual(recorder.observations, [])


class SchemaMigrationTests(unittest.TestCase):
    def test_observer_id_added_to_pre_anchor_database(self):
        """A DB created by the previous ble-sensor gains the new columns."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "old.db")
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE ble_device_observations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        captured_at_utc TEXT NOT NULL,
                        pi_id TEXT NOT NULL,
                        vehicle_id TEXT NOT NULL,
                        tracked_mac_normalized TEXT NOT NULL,
                        rssi INTEGER,
                        latitude REAL,
                        longitude REAL,
                        gps_timestamp TEXT,
                        gps_age_seconds INTEGER
                    );
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE tracked_asset_resolutions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        captured_at_utc TEXT NOT NULL,
                        tracked_mac_normalized TEXT NOT NULL,
                        vehicle_id TEXT NOT NULL,
                        resolved_pi_id TEXT,
                        resolved_latitude REAL,
                        resolved_longitude REAL,
                        resolution_method TEXT NOT NULL,
                        candidate_count INTEGER NOT NULL,
                        confidence_score REAL NOT NULL
                    );
                    """
                )
            main._init_local_store(db_path)  # must not raise, must migrate
            with sqlite3.connect(db_path) as conn:
                obs_cols = {r[1] for r in conn.execute("PRAGMA table_info(ble_device_observations)")}
                res_cols = {r[1] for r in conn.execute("PRAGMA table_info(tracked_asset_resolutions)")}
                tables = {
                    r[0]
                    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            self.assertIn("observer_id", obs_cols)
            self.assertIn("nearest_observer_id", res_cols)
            self.assertIn("observer_count", res_cols)
            self.assertIn("anchor_status", tables)

    def test_init_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "new.db")
            main._init_local_store(db_path)
            main._init_local_store(db_path)


class ObserverRankingTests(unittest.TestCase):
    def test_strongest_median_wins(self):
        candidates = [
            {"observer_id": "anchor-aaaaaa", "rssi": -80},
            {"observer_id": "anchor-aaaaaa", "rssi": -82},
            {"observer_id": "anchor-bbbbbb", "rssi": -55},
            {"observer_id": "anchor-bbbbbb", "rssi": -60},
            {"observer_id": "pi-test-01", "rssi": -70},
        ]
        nearest, count = main._rank_observers(candidates)
        self.assertEqual(nearest, "anchor-bbbbbb")
        self.assertEqual(count, 3)

    def test_median_beats_single_outlier_max(self):
        # anchor-a has one loud outlier but is generally far; anchor-b is
        # consistently closer. Median must pick anchor-b.
        candidates = (
            [{"observer_id": "anchor-aaaaaa", "rssi": -40}]
            + [{"observer_id": "anchor-aaaaaa", "rssi": -85}] * 4
            + [{"observer_id": "anchor-bbbbbb", "rssi": -60}] * 5
        )
        nearest, _ = main._rank_observers(candidates)
        self.assertEqual(nearest, "anchor-bbbbbb")

    def test_deterministic_tiebreak_by_observer_id(self):
        candidates = [
            {"observer_id": "anchor-bbbbbb", "rssi": -60},
            {"observer_id": "anchor-aaaaaa", "rssi": -60},
        ]
        nearest, count = main._rank_observers(candidates)
        self.assertEqual(nearest, "anchor-aaaaaa")
        self.assertEqual(count, 2)

    def test_no_usable_candidates(self):
        self.assertEqual(main._rank_observers([]), (None, 0))
        self.assertEqual(
            main._rank_observers([{"observer_id": "x", "rssi": None}]), (None, 0)
        )


class ResolverV2IntegrationTests(unittest.TestCase):
    def test_resolution_row_carries_nearest_observer(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "ble-sensor.db")
            main._init_local_store(db_path)
            mac = "A4:C1:38:AA:BB:CC"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT INTO tracked_asset_registry (tracked_mac_normalized, label, active) VALUES (?, ?, 1)",
                    (mac, "Cooler #7"),
                )
                now = "2026-07-01T12:00:00+00:00"
                rows = [
                    (now, "pi-test-01", "TRK-905", mac, -70, 45.0, -122.0, now, 5, "pi-test-01"),
                    (now, "pi-test-01", "TRK-905", mac, -50, 45.0, -122.0, now, 5, "anchor-bbbbbb"),
                    (now, "pi-test-01", "TRK-905", mac, -85, 45.0, -122.0, now, 5, "anchor-aaaaaa"),
                ]
                conn.executemany(
                    """
                    INSERT INTO ble_device_observations (
                        captured_at_utc, pi_id, vehicle_id, tracked_mac_normalized, rssi,
                        latitude, longitude, gps_timestamp, gps_age_seconds, observer_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

            config = main.load_config()
            config = main.Config(**{**config.__dict__, "local_db_path": db_path})
            main._resolve_tracked_asset_positions(config)

            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT nearest_observer_id, observer_count
                    FROM tracked_asset_resolutions
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
            self.assertEqual(row[0], "anchor-bbbbbb")
            self.assertEqual(row[1], 3)


if __name__ == "__main__":
    unittest.main()
