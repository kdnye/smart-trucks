"""Stationary-aware GPS track filtering for the edge collector.

A stationary GPS receiver still reports a position that wanders by tens of
metres as satellite geometry and multipath change. Plotted on a map this reads
as a "starburst" of lines radiating from the parked spot. Two things make it
worse on this fleet:

* the on-map track is reconstructed from points the edge emits, so every drifted
  fix we forward becomes a vertex the dashboard faithfully draws, and
* heartbeats were the *only* GPS source (one fix per ``HEARTBEAT_INTERVAL``), so
  a moving truck's track was also far too coarse.

``GpsTrackFilter`` is a small two-state machine (STATIONARY / MOVING) that solves
both at the source:

* **STATIONARY** — the reported position is *pinned* to the anchor (where the
  vehicle stopped) and no track point is emitted, so the starburst collapses to
  a single point. A truck only leaves this state when GPS speed says it's moving
  **and** it has physically displaced beyond the drift radius — so pure GPS
  *speed noise* while parked can't kick off a flood of drifted points.
* **MOVING** — every fix is emitted, giving a dense track (the collector samples
  ~1 Hz while moving). The truck returns to STATIONARY when speed drops below the
  threshold, or when it stops making real progress for ``stationary_hold_seconds``
  (covers speed noise that persists after the vehicle has actually stopped).

The class is pure (no I/O, monotonic clock injected) so the behaviour can be
unit tested deterministically. The collector reads ``GpsFilterResult.moving`` to
sample fast while moving and slow while parked.
"""

import math
from dataclasses import dataclass

from shared.env import read_float_env

EARTH_RADIUS_M = 6_371_000.0

_STATIONARY = "stationary"
_MOVING = "moving"


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 coordinates, in metres."""
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    # Clamp: floating-point rounding can nudge `a` just past 1.0, which would make
    # 1 - a negative and raise "math domain error" in sqrt.
    a = min(1.0, max(0.0, a))
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@dataclass(frozen=True)
class GpsFilterConfig:
    """Tunable thresholds for :class:`GpsTrackFilter`.

    All are overridable via environment variables so the fleet can be re-tuned
    without a code change.
    """

    # At or above this speed the vehicle is treated as moving.
    stationary_speed_kmh: float = 5.0
    # A parked fix must displace beyond this radius (and read as moving) before we
    # leave STATIONARY — rejects multipath drift and GPS speed noise.
    stationary_radius_m: float = 20.0
    # While MOVING, "real progress" means advancing at least this far; if the
    # vehicle stays within it for stationary_hold_seconds it's treated as stopped.
    min_track_distance_m: float = 8.0
    # How long without real progress (while speed still reads as moving) before we
    # declare the vehicle stopped and return to STATIONARY.
    stationary_hold_seconds: float = 8.0
    # Reject fixes whose HDOP exceeds this (0 disables the check). A poor-HDOP fix
    # is the main source of stationary drift, so it must not move the anchor.
    max_hdop: float = 0.0
    # While STATIONARY, emit one pinned point every N seconds so a long dwell still
    # leaves a breadcrumb (0 disables; heartbeats already prove liveness).
    keepalive_seconds: float = 0.0

    @classmethod
    def from_env(cls) -> "GpsFilterConfig":
        return cls(
            stationary_speed_kmh=read_float_env("GPS_STATIONARY_SPEED_KMH", cls.stationary_speed_kmh, minimum=0.0),
            stationary_radius_m=read_float_env("GPS_STATIONARY_RADIUS_M", cls.stationary_radius_m, minimum=0.0),
            min_track_distance_m=read_float_env("GPS_MIN_TRACK_DISTANCE_M", cls.min_track_distance_m, minimum=0.0),
            stationary_hold_seconds=read_float_env("GPS_STATIONARY_HOLD_SECONDS", cls.stationary_hold_seconds, minimum=0.0),
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
    # True while the vehicle is in the MOVING state — the collector samples fast
    # (~1 Hz) when set, and throttles back when clear.
    moving: bool
    # Short machine-readable reason, for logging/diagnostics.
    reason: str


class GpsTrackFilter:
    """Decide, per fix, what to report live and whether to log a track point."""

    def __init__(self, config: GpsFilterConfig | None = None) -> None:
        self.config = config or GpsFilterConfig()
        self._state: str | None = None
        self._anchor_lat: float | None = None
        self._anchor_lon: float | None = None
        # Last position where real progress was made (MOVING-state stop detection).
        self._progress_lat: float | None = None
        self._progress_lon: float | None = None
        self._last_progress_monotonic: float = 0.0
        self._last_emit_monotonic: float | None = None

    @property
    def moving(self) -> bool:
        return self._state == _MOVING

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

        # A poor-quality fix must never establish or move the anchor. Report the
        # pinned anchor if we have one (else the raw fix so the live map isn't
        # blank), but never emit or change state on it.
        if cfg.max_hdop > 0 and hdop is not None and hdop > cfg.max_hdop:
            if self._anchor_lat is not None and self._anchor_lon is not None:
                return GpsFilterResult(
                    self._anchor_lat, self._anchor_lon, speed,
                    pinned=True, emit=False, moving=self.moving, reason="hdop_reject_pinned",
                )
            return GpsFilterResult(
                latitude, longitude, speed,
                pinned=False, emit=False, moving=self.moving, reason="hdop_reject_no_anchor",
            )

        # First valid fix: anchor here, start parked, always emitted.
        if self._state is None:
            self._state = _STATIONARY
            self._anchor_lat, self._anchor_lon = latitude, longitude
            self._last_emit_monotonic = now_monotonic
            return GpsFilterResult(
                latitude, longitude, speed,
                pinned=False, emit=True, moving=False, reason="first_fix",
            )

        moving_speed = speed >= cfg.stationary_speed_kmh

        if self._state == _STATIONARY:
            distance_m = haversine_m(self._anchor_lat, self._anchor_lon, latitude, longitude)
            # Confirmed departure needs BOTH a moving speed AND real displacement
            # beyond the drift radius — so speed noise alone can't start a flood.
            if moving_speed and distance_m > cfg.stationary_radius_m:
                self._state = _MOVING
                self._progress_lat, self._progress_lon = latitude, longitude
                self._last_progress_monotonic = now_monotonic
                self._last_emit_monotonic = now_monotonic
                return GpsFilterResult(
                    latitude, longitude, speed,
                    pinned=False, emit=True, moving=True, reason="depart",
                )
            # Still parked: pin to the anchor; optional keep-alive breadcrumb.
            emit_keepalive = (
                cfg.keepalive_seconds > 0
                and self._last_emit_monotonic is not None
                and (now_monotonic - self._last_emit_monotonic) >= cfg.keepalive_seconds
            )
            if emit_keepalive:
                self._last_emit_monotonic = now_monotonic
            return GpsFilterResult(
                self._anchor_lat, self._anchor_lon, speed,
                pinned=True, emit=emit_keepalive, moving=False,
                reason="keepalive" if emit_keepalive else "pinned",
            )

        # MOVING: emit every fix for a dense track. Track real progress so a fix
        # that stops advancing (e.g. stopped at a light with noisy speed) returns
        # us to STATIONARY rather than emitting drift.
        progressed = haversine_m(self._progress_lat, self._progress_lon, latitude, longitude)
        if progressed >= cfg.min_track_distance_m:
            self._progress_lat, self._progress_lon = latitude, longitude
            self._last_progress_monotonic = now_monotonic
        no_progress = (now_monotonic - self._last_progress_monotonic) >= cfg.stationary_hold_seconds
        if not moving_speed or no_progress:
            # Came to rest: anchor here and start pinning. Emit this final point so
            # the trail reaches the stopping spot.
            self._state = _STATIONARY
            self._anchor_lat, self._anchor_lon = latitude, longitude
            self._last_emit_monotonic = now_monotonic
            return GpsFilterResult(
                latitude, longitude, speed,
                pinned=False, emit=True, moving=False, reason="arrive",
            )
        self._last_emit_monotonic = now_monotonic
        return GpsFilterResult(
            latitude, longitude, speed,
            pinned=False, emit=True, moving=True, reason="moving",
        )
