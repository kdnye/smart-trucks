import importlib.util
import sys
import unittest
from pathlib import Path

EDGE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = EDGE_DIR.parent
# gps_filter imports `shared.env`; make both the edge dir and the repo root
# importable so the test runs from any working directory.
for path in (str(EDGE_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

MODULE_PATH = EDGE_DIR / "gps_filter.py"
SPEC = importlib.util.spec_from_file_location("gps_filter", MODULE_PATH)
gps_filter = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(gps_filter)

GpsTrackFilter = gps_filter.GpsTrackFilter
GpsFilterConfig = gps_filter.GpsFilterConfig
haversine_m = gps_filter.haversine_m

# ~1 metre north/east of (33.0, -84.0) in degrees (1 deg lat ≈ 111.32 km).
_M_PER_DEG_LAT = 111_320.0
ANCHOR_LAT = 33.0
ANCHOR_LON = -84.0


def _lat_offset(meters: float) -> float:
    return ANCHOR_LAT + meters / _M_PER_DEG_LAT


class HaversineTests(unittest.TestCase):
    def test_known_short_distance(self) -> None:
        # 0.0009 deg latitude ≈ 100 m.
        d = haversine_m(33.0, -84.0, 33.0009, -84.0)
        self.assertAlmostEqual(d, 100.0, delta=1.0)


class StationaryPinningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = GpsTrackFilter(
            GpsFilterConfig(
                stationary_speed_kmh=5.0,
                stationary_radius_m=20.0,
                min_track_distance_m=8.0,
            )
        )

    def test_first_fix_emits_and_is_not_pinned(self) -> None:
        r = self.f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=0.9, now_monotonic=0.0)
        self.assertTrue(r.emit)
        self.assertFalse(r.pinned)
        self.assertEqual(r.reason, "first_fix")
        self.assertEqual((r.latitude, r.longitude), (ANCHOR_LAT, ANCHOR_LON))

    def test_drift_within_radius_is_pinned_and_not_emitted(self) -> None:
        self.f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=0.9, now_monotonic=0.0)
        # Wander ~10 m while stationary — within the 20 m radius.
        r = self.f.process(latitude=_lat_offset(10.0), longitude=ANCHOR_LON, speed_kmh=0.4, hdop=1.2, now_monotonic=5.0)
        self.assertFalse(r.emit)
        self.assertTrue(r.pinned)
        self.assertEqual(r.reason, "pinned")
        # Reports the anchor, not the drifted coordinate.
        self.assertEqual((r.latitude, r.longitude), (ANCHOR_LAT, ANCHOR_LON))

    def test_moving_emits_raw_position(self) -> None:
        self.f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=0.9, now_monotonic=0.0)
        r = self.f.process(latitude=_lat_offset(10.0), longitude=ANCHOR_LON, speed_kmh=50.0, hdop=1.0, now_monotonic=5.0)
        self.assertTrue(r.emit)
        self.assertFalse(r.pinned)
        self.assertEqual(r.reason, "moving")
        self.assertAlmostEqual(r.latitude, _lat_offset(10.0))

    def test_leaving_radius_while_slow_emits_and_moves_anchor(self) -> None:
        self.f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=0.9, now_monotonic=0.0)
        # 30 m away while slow — outside the 20 m radius.
        r = self.f.process(latitude=_lat_offset(30.0), longitude=ANCHOR_LON, speed_kmh=1.0, hdop=1.0, now_monotonic=5.0)
        self.assertTrue(r.emit)
        self.assertEqual(r.reason, "left_radius")
        # Anchor advanced: a subsequent nearby fix is now pinned to the new spot.
        r2 = self.f.process(latitude=_lat_offset(33.0), longitude=ANCHOR_LON, speed_kmh=0.0, hdop=1.0, now_monotonic=10.0)
        self.assertTrue(r2.pinned)
        self.assertAlmostEqual(r2.latitude, _lat_offset(30.0))


class HdopGatingTests(unittest.TestCase):
    def test_poor_hdop_with_anchor_is_pinned_not_emitted(self) -> None:
        f = GpsTrackFilter(GpsFilterConfig(max_hdop=2.5))
        f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=1.0, now_monotonic=0.0)
        r = f.process(latitude=_lat_offset(200.0), longitude=ANCHOR_LON, speed_kmh=0.0, hdop=8.0, now_monotonic=5.0)
        self.assertFalse(r.emit)
        self.assertTrue(r.pinned)
        self.assertEqual(r.reason, "hdop_reject_pinned")
        # The bad fix did NOT move the anchor.
        self.assertEqual((r.latitude, r.longitude), (ANCHOR_LAT, ANCHOR_LON))

    def test_poor_hdop_without_anchor_does_not_anchor(self) -> None:
        f = GpsTrackFilter(GpsFilterConfig(max_hdop=2.5))
        r = f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=9.0, now_monotonic=0.0)
        self.assertFalse(r.emit)
        self.assertFalse(r.pinned)
        self.assertEqual(r.reason, "hdop_reject_no_anchor")

    def test_hdop_check_disabled_when_max_is_zero(self) -> None:
        f = GpsTrackFilter(GpsFilterConfig(max_hdop=0.0))
        r = f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=50.0, now_monotonic=0.0)
        self.assertTrue(r.emit)  # huge HDOP ignored when the gate is off


class DeadbandAndKeepaliveTests(unittest.TestCase):
    def test_below_min_spacing_does_not_emit(self) -> None:
        # radius < min_distance makes the "outside radius but under spacing" path
        # reachable: an 8 m slow nudge clears the 5 m radius but not the 15 m spacing.
        f = GpsTrackFilter(
            GpsFilterConfig(stationary_speed_kmh=5.0, stationary_radius_m=5.0, min_track_distance_m=15.0)
        )
        f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=1.0, now_monotonic=0.0)
        r = f.process(latitude=_lat_offset(8.0), longitude=ANCHOR_LON, speed_kmh=1.0, hdop=1.0, now_monotonic=5.0)
        self.assertFalse(r.emit)
        self.assertEqual(r.reason, "below_min_spacing")

    def test_keepalive_emits_pinned_breadcrumb(self) -> None:
        f = GpsTrackFilter(
            GpsFilterConfig(stationary_radius_m=20.0, keepalive_seconds=60.0)
        )
        f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=1.0, now_monotonic=0.0)
        # Stationary, but 90 s elapsed — emit a single pinned breadcrumb.
        r = f.process(latitude=_lat_offset(3.0), longitude=ANCHOR_LON, speed_kmh=0.0, hdop=1.0, now_monotonic=90.0)
        self.assertTrue(r.emit)
        self.assertTrue(r.pinned)
        self.assertEqual(r.reason, "keepalive")
        self.assertEqual((r.latitude, r.longitude), (ANCHOR_LAT, ANCHOR_LON))
        # Immediately after, still pinned but no second breadcrumb.
        r2 = f.process(latitude=_lat_offset(3.0), longitude=ANCHOR_LON, speed_kmh=0.0, hdop=1.0, now_monotonic=95.0)
        self.assertFalse(r2.emit)
        self.assertEqual(r2.reason, "pinned")


if __name__ == "__main__":
    unittest.main()
