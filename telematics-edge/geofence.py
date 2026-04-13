import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from nmea_reader import GpsReading

logger = logging.getLogger(__name__)


class GeofenceEngine:
    """Evaluate geofence enter/exit events using a lightweight ray-casting algorithm."""

    def __init__(self, geofence_file: str = "/data/geofences.json") -> None:
        self.geofence_file = Path(geofence_file)
        self.geofences: dict[str, dict] = {}
        self.active_geofences: set[str] = set()
        self.load_geofences()

    def load_geofences(self) -> None:
        """Load geofence polygons from local JSON storage."""
        if not self.geofence_file.exists():
            logger.warning("Geofence file %s not found. Running without geofences.", self.geofence_file)
            return

        try:
            with self.geofence_file.open("r", encoding="utf-8") as file:
                self.geofences = json.load(file)
            logger.info("Successfully loaded %d geofences into edge memory.", len(self.geofences))
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Error loading geofence file: %s", exc)

    def _is_point_in_polygon(self, lat: float, lon: float, polygon: list[list[float]]) -> bool:
        """Return True when the point falls inside polygon bounds via ray casting."""
        x, y = lon, lat
        inside = False

        if len(polygon) < 3:
            return False

        p1x, p1y = polygon[0][1], polygon[0][0]
        for i in range(len(polygon) + 1):
            p2x, p2y = polygon[i % len(polygon)][1], polygon[i % len(polygon)][0]
            if y > min(p1y, p2y):
                if y <= max(p1y, p2y):
                    if x <= max(p1x, p2x):
                        if p1y != p2y:
                            xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                        if p1x == p2x or x <= xinters:
                            inside = not inside
            p1x, p1y = p2x, p2y

        return inside

    async def evaluate_location(
        self,
        reading: GpsReading,
        event_callback: Callable[[str, dict], Awaitable[None]],
    ) -> None:
        """Emit geofence enter/exit events when the current location changes state."""
        if reading.latitude is None or reading.longitude is None or reading.fix_type == 1:
            return

        current_time = datetime.now(timezone.utc).isoformat()
        currently_inside: set[str] = set()

        for fence_id, fence_data in self.geofences.items():
            polygon = fence_data.get("polygon", [])
            if self._is_point_in_polygon(reading.latitude, reading.longitude, polygon):
                currently_inside.add(fence_id)

        new_entries = currently_inside - self.active_geofences
        for fence_id in new_entries:
            fence_name = self.geofences[fence_id].get("name", "Unknown Yard")
            logger.info("📍 ENTERED Geofence: %s (%s)", fence_name, fence_id)
            await event_callback(
                "geofence_enter",
                {
                    "geofence_id": fence_id,
                    "geofence_name": fence_name,
                    "lat": reading.latitude,
                    "lon": reading.longitude,
                    "occurred_at": current_time,
                },
            )

        new_exits = self.active_geofences - currently_inside
        for fence_id in new_exits:
            fence_name = self.geofences[fence_id].get("name", "Unknown Yard")
            logger.info("📍 EXITED Geofence: %s (%s)", fence_name, fence_id)
            await event_callback(
                "geofence_exit",
                {
                    "geofence_id": fence_id,
                    "geofence_name": fence_name,
                    "lat": reading.latitude,
                    "lon": reading.longitude,
                    "occurred_at": current_time,
                },
            )

        self.active_geofences = currently_inside
