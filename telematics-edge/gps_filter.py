"""Stationary-aware GPS track filtering for the edge collector.

A stationary GPS receiver still reports a position that wanders by tens of
metres as satellite geometry and multipath change. Plotted on a map this reads
as a "starburst" of lines radiating from the parked spot. Two things make it
worse on this fleet:

* the on-map track is reconstructed from points the edge emits, so every drifted
  fix we forward becomes a vertex the dashboard faithfully draws, and
* heartbeats were the *only* GPS source (one fix per ``HEARTBEAT_INTERVAL``), so
  a moving truck's track was also far too coarse.

``GpsTrackFilter`` solves both at the source:

* **Coordinate pinning** — while the vehicle is stationary (low speed and within
  a drift radius of an established anchor) the *anchor* coordinate is reported
  instead of the drifting raw fix, and no new track point is emitted. The
  starburst collapses to a single point.
* **Distance-or-time deadband** — while moving, a point is emitted whenever the
  vehicle has travelled a meaningful distance, giving a dense, even track. An
  optional keep-alive emits a single pinned point on a slow cadence so a long
  dwell is still visible.
* **HDOP gating** — fixes with a poor horizontal dilution of precision (the main
  driver of stationary drift) never move the anchor.

The class is intentionally pure (no I/O, monotonic clock injected) so the
behaviour can be unit tested deterministically.
"""

import math
from dataclasses import dataclass

from shared.env import read_float_env

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 coordinates, in metres."""
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@dataclass(frozen=True)
class GpsFilterConfig:
    """Tunable thresholds for :class:`GpsTrackFilter`.

    Defaults are conservative for a parked truck on a noisy urban multipath
    fix; all are overridable via environment variables so the fleet can be
    re-tuned without a code change.
    """

    # At or above this speed the vehicle is treated as moving (never pinned).
    stationary_speed_kmh: float = 5.0
    # While slow, fixes within this distance of the anchor are treated as drift.
    stationary_radius_m: float = 20.0
    # While moving, emit a track point once the vehicle has travelled this far
    # from the last emitted point (keeps a dense but not redundant track).
    min_track_distance_m: float = 8.0
    # Reject fixes whose HDOP exceeds this (0 disables the check). A poor-HDOP
    # fix is the main source of stationary drift, so it must not move the anchor.
    max_hdop: float = 0.0
    # While stationary, emit one pinned point every N seconds so a long dwell
    # still leaves a breadcrumb (0 disables; heartbeats already prove liveness).
    keepalive_seconds: float = 0.0

    @classmethod
    def from_env(cls) -> "GpsFilterConfig":
        return cls(
            stationary_speed_kmh=read_float_env("GPS_STATIONARY_SPEED_KMH", cls.stationary_speed_kmh, minimum=0.0),
            stationary_radius_m=read_float_env("GPS_STATIONARY_RADIUS_M", cls.stationary_radius_m, minimum=0.0),
            min_track_distance_m=read_float_env("GPS_MIN_TRACK_DISTANCE_M", cls.min_track_distance_m, minimum=0.0),
            max_hdop=read_float_env("GPS_MAX_HDOP", cls.max_hdop, minimum=0.0),
            keepalive_seconds=read_float_env("GPS_TRACK_KEEPALIVE_SECONDS", cls.keepalive_seconds, minimum=0.0),
        )


@dataclass(frozen=True)
class GpsFilterResult:
    """Outcome of feeding one fix to the filter."""

    # Stabilized position to report to live consumers (heartbeat / live map).
    # Equals the anchor while pinned, the raw fix while moving.
    latitude: float
    longitude: float
    speed_kmh: float
    # True when the reported coordinate is the pinned anchor (drift suppressed).
    pinned: bool
    # True when a track point should be persisted for this fix.
    emit: bool
    # Short machine-readable reason, for logging/diagnostics.
    reason: str


class GpsTrackFilter:
    """Decide, per fix, what to report live and whether to log a track point."""

    def __init__(self, config: GpsFilterConfig | None = None) -> None:
        self.config = config or GpsFilterConfig()
        self._anchor_lat: float | None = None
        self._anchor_lon: float | None = None
        self._last_emit_monotonic: float | None = None

    def _emit(self, lat: float, lon: float, now: float) -> None:
        self._anchor_lat = lat
        self._anchor_lon = lon
        self._last_emit_monotonic = now

    def process(
        self,
        *,
        latitude: float,
        longitude: float,
        speed_kmh: float | None,
        hdop: float | None,
        now_monotonic: float,
    ) -> GpsFilterResult:
        cfg = self.config
        speed = speed_kmh or 0.0

        # A poor-quality fix must never establish or move the anchor. If we have
        # an anchor, report it (pinned) and drop the fix; otherwise report the
        # raw fix so the live map isn't blank, but still don't anchor on it.
        if cfg.max_hdop > 0 and hdop is not None and hdop > cfg.max_hdop:
            if self._anchor_lat is not None and self._anchor_lon is not None:
                return GpsFilterResult(
                    latitude=self._anchor_lat,
                    longitude=self._anchor_lon,
                    speed_kmh=speed,
                    pinned=True,
                    emit=False,
                    reason="hdop_reject_pinned",
                )
            return GpsFilterResult(
                latitude=latitude,
                longitude=longitude,
                speed_kmh=speed,
                pinned=False,
                emit=False,
                reason="hdop_reject_no_anchor",
            )

        # First valid fix establishes the anchor and is always emitted.
        if self._anchor_lat is None or self._anchor_lon is None:
            self._emit(latitude, longitude, now_monotonic)
            return GpsFilterResult(
                latitude=latitude,
                longitude=longitude,
                speed_kmh=speed,
                pinned=False,
                emit=True,
                reason="first_fix",
            )

        distance_m = haversine_m(self._anchor_lat, self._anchor_lon, latitude, longitude)
        moving = speed >= cfg.stationary_speed_kmh

        # Real movement: the vehicle is moving, or it has stepped outside the
        # stationary drift radius. Report the raw fix and (when it has covered
        # the minimum spacing) emit a track point, advancing the anchor.
        if moving or distance_m > cfg.stationary_radius_m:
            if moving or distance_m >= cfg.min_track_distance_m:
                self._emit(latitude, longitude, now_monotonic)
                return GpsFilterResult(
                    latitude=latitude,
                    longitude=longitude,
                    speed_kmh=speed,
                    pinned=False,
                    emit=True,
                    reason="moving" if moving else "left_radius",
                )
            # Outside the radius but a sub-spacing nudge while slow: report raw
            # but don't spam a point.
            return GpsFilterResult(
                latitude=latitude,
                longitude=longitude,
                speed_kmh=speed,
                pinned=False,
                emit=False,
                reason="below_min_spacing",
            )

        # Stationary within the drift radius: pin to the anchor. Optionally drop
        # a keep-alive breadcrumb on a slow cadence.
        emit_keepalive = (
            cfg.keepalive_seconds > 0
            and self._last_emit_monotonic is not None
            and (now_monotonic - self._last_emit_monotonic) >= cfg.keepalive_seconds
        )
        if emit_keepalive:
            self._last_emit_monotonic = now_monotonic
        return GpsFilterResult(
            latitude=self._anchor_lat,
            longitude=self._anchor_lon,
            speed_kmh=speed,
            pinned=True,
            emit=emit_keepalive,
            reason="keepalive" if emit_keepalive else "pinned",
        )
