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

_M_PER_DEG_LAT = 111_320.0
ANCHOR_LAT = 33.0
ANCHOR_LON = -84.0


def _lat_offset(meters: float) -> float:
    return ANCHOR_LAT + meters / _M_PER_DEG_LAT


def _config(**overrides) -> "GpsFilterConfig":
    base = dict(
        stationary_speed_kmh=5.0,
        stationary_radius_m=20.0,
        min_track_distance_m=8.0,
        stationary_hold_seconds=8.0,
    )
    base.update(overrides)
    return GpsFilterConfig(**base)


class HaversineTests(unittest.TestCase):
    def test_identical_points_is_zero(self) -> None:
        # Identical inputs drive a -> 0; the clamp must keep sqrt(1-a) finite.
        self.assertEqual(haversine_m(33.0, -84.0, 33.0, -84.0), 0.0)

    def test_known_short_distance(self) -> None:
        d = haversine_m(33.0, -84.0, 33.0009, -84.0)
        self.assertAlmostEqual(d, 100.0, delta=1.0)


class StationaryPinningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = GpsTrackFilter(_config())

    def test_first_fix_emits_and_is_not_pinned(self) -> None:
        r = self.f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=0.9, now_monotonic=0.0)
        self.assertTrue(r.emit)
        self.assertFalse(r.pinned)
        self.assertFalse(r.moving)
        self.assertEqual(r.reason, "first_fix")

    def test_drift_within_radius_is_pinned_and_not_emitted(self) -> None:
        self.f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=0.9, now_monotonic=0.0)
        r = self.f.process(latitude=_lat_offset(10.0), longitude=ANCHOR_LON, speed_kmh=0.4, hdop=1.2, now_monotonic=5.0)
        self.assertFalse(r.emit)
        self.assertTrue(r.pinned)
        self.assertFalse(r.moving)
        self.assertEqual((r.latitude, r.longitude), (ANCHOR_LAT, ANCHOR_LON))

    def test_speed_noise_within_radius_stays_pinned(self) -> None:
        # Reviewer case: GPS speed reads "moving" while the truck is parked and
        # has NOT displaced beyond the drift radius. Must stay pinned, not flood.
        self.f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=0.9, now_monotonic=0.0)
        r = self.f.process(latitude=_lat_offset(10.0), longitude=ANCHOR_LON, speed_kmh=40.0, hdop=1.0, now_monotonic=1.0)
        self.assertFalse(r.emit)
        self.assertTrue(r.pinned)
        self.assertFalse(r.moving)
        self.assertEqual((r.latitude, r.longitude), (ANCHOR_LAT, ANCHOR_LON))


class MovingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = GpsTrackFilter(_config())
        self.f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=0.9, now_monotonic=0.0)

    def _depart(self, t: float = 1.0):
        # Speed + displacement beyond the 20 m radius confirms a departure.
        return self.f.process(latitude=_lat_offset(30.0), longitude=ANCHOR_LON, speed_kmh=50.0, hdop=1.0, now_monotonic=t)

    def test_departure_requires_speed_and_displacement(self) -> None:
        r = self._depart()
        self.assertTrue(r.emit)
        self.assertTrue(r.moving)
        self.assertEqual(r.reason, "depart")
        self.assertAlmostEqual(r.latitude, _lat_offset(30.0))

    def test_moving_emits_every_fix(self) -> None:
        self._depart(1.0)
        r2 = self.f.process(latitude=_lat_offset(60.0), longitude=ANCHOR_LON, speed_kmh=50.0, hdop=1.0, now_monotonic=2.0)
        r3 = self.f.process(latitude=_lat_offset(90.0), longitude=ANCHOR_LON, speed_kmh=50.0, hdop=1.0, now_monotonic=3.0)
        self.assertTrue(r2.emit and r2.moving)
        self.assertTrue(r3.emit and r3.moving)
        self.assertEqual(r3.reason, "moving")

    def test_slow_speed_drop_returns_to_stationary(self) -> None:
        self._depart(1.0)
        r = self.f.process(latitude=_lat_offset(60.0), longitude=ANCHOR_LON, speed_kmh=2.0, hdop=1.0, now_monotonic=2.0)
        self.assertTrue(r.emit)
        self.assertFalse(r.moving)
        self.assertEqual(r.reason, "arrive")
        # Anchor reset to the stopping spot: a nearby fix is now pinned there.
        r2 = self.f.process(latitude=_lat_offset(63.0), longitude=ANCHOR_LON, speed_kmh=0.0, hdop=1.0, now_monotonic=3.0)
        self.assertTrue(r2.pinned)
        self.assertAlmostEqual(r2.latitude, _lat_offset(60.0))

    def test_no_progress_while_speed_noisy_returns_to_stationary(self) -> None:
        # Departs, then "speed" stays high but position barely moves for the hold
        # window — treated as stopped (speed noise), not an endless drift flood.
        self._depart(1.0)
        reasons = []
        for t in range(2, 12):  # within 8 m of the progress point, speed high
            r = self.f.process(latitude=_lat_offset(31.0), longitude=ANCHOR_LON, speed_kmh=50.0, hdop=1.0, now_monotonic=float(t))
            reasons.append(r.reason)
        # It transitions to a stop ("arrive") once the hold window elapses with no
        # real progress, then stays pinned — never an endless drift emit.
        self.assertIn("arrive", reasons)
        self.assertFalse(r.moving)
        self.assertEqual(r.reason, "pinned")


class HdopGatingTests(unittest.TestCase):
    def test_poor_hdop_with_anchor_is_pinned_not_emitted(self) -> None:
        f = GpsTrackFilter(_config(max_hdop=2.5))
        f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=1.0, now_monotonic=0.0)
        r = f.process(latitude=_lat_offset(200.0), longitude=ANCHOR_LON, speed_kmh=0.0, hdop=8.0, now_monotonic=5.0)
        self.assertFalse(r.emit)
        self.assertTrue(r.pinned)
        self.assertEqual(r.reason, "hdop_reject_pinned")
        self.assertEqual((r.latitude, r.longitude), (ANCHOR_LAT, ANCHOR_LON))

    def test_hdop_check_disabled_when_max_is_zero(self) -> None:
        f = GpsTrackFilter(_config(max_hdop=0.0))
        r = f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=50.0, now_monotonic=0.0)
        self.assertTrue(r.emit)


class KeepaliveTests(unittest.TestCase):
    def test_keepalive_emits_pinned_breadcrumb(self) -> None:
        f = GpsTrackFilter(_config(keepalive_seconds=60.0))
        f.process(latitude=ANCHOR_LAT, longitude=ANCHOR_LON, speed_kmh=0.0, hdop=1.0, now_monotonic=0.0)
        r = f.process(latitude=_lat_offset(3.0), longitude=ANCHOR_LON, speed_kmh=0.0, hdop=1.0, now_monotonic=90.0)
        self.assertTrue(r.emit)
        self.assertTrue(r.pinned)
        self.assertEqual(r.reason, "keepalive")
        self.assertEqual((r.latitude, r.longitude), (ANCHOR_LAT, ANCHOR_LON))
        r2 = f.process(latitude=_lat_offset(3.0), longitude=ANCHOR_LON, speed_kmh=0.0, hdop=1.0, now_monotonic=95.0)
        self.assertFalse(r2.emit)
        self.assertEqual(r2.reason, "pinned")


if __name__ == "__main__":
    unittest.main()
