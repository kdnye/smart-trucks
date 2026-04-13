import logging
import math
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable, Optional

from nmea_reader import GpsReading

logger = logging.getLogger(__name__)


class VehicleState(Enum):
    STOPPED = "STOPPED"
    IDLE = "IDLE"
    MOVING = "MOVING"


class TripTracker:
    def __init__(self, speed_threshold_kmh: float = 5.0, idle_timeout_sec: float = 180.0) -> None:
        self.state = VehicleState.STOPPED
        self.speed_threshold = speed_threshold_kmh
        self.idle_timeout = idle_timeout_sec

        self.current_trip_id: Optional[str] = None
        self.trip_distance_km = 0.0
        self.total_odometer_km = 0.0

        self.last_reading: Optional[GpsReading] = None
        self.idle_start_time: Optional[datetime] = None

    def _haversine_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance in kilometers between two GPS coordinates."""
        earth_radius_km = 6371.0

        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)

        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(math.radians(lat1))
            * math.cos(math.radians(lat2))
            * math.sin(dlon / 2) ** 2
        )

        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return earth_radius_km * c

    async def process_gps_reading(
        self,
        reading: GpsReading,
        event_callback: Callable[[str, dict], Awaitable[None]],
    ) -> None:
        """Update state machine from a GPS reading and emit lifecycle events."""
        if reading.latitude is None or reading.longitude is None or reading.fix_type == 1:
            return

        current_time = datetime.now(timezone.utc)
        speed = reading.speed_kmh or 0.0

        if (
            self.last_reading
            and self.last_reading.latitude is not None
            and self.last_reading.longitude is not None
        ):
            dist_km = self._haversine_distance(
                self.last_reading.latitude,
                self.last_reading.longitude,
                reading.latitude,
                reading.longitude,
            )
            if self.state == VehicleState.MOVING:
                self.trip_distance_km += dist_km
                self.total_odometer_km += dist_km

        self.last_reading = reading

        if self.state == VehicleState.STOPPED:
            if speed >= self.speed_threshold:
                self.state = VehicleState.MOVING
                self.current_trip_id = str(uuid.uuid4())
                self.trip_distance_km = 0.0

                logger.info("Started new trip: %s", self.current_trip_id)
                await event_callback(
                    "trip_start",
                    {
                        "trip_id": self.current_trip_id,
                        "odometer_km": self.total_odometer_km,
                        "lat": reading.latitude,
                        "lon": reading.longitude,
                    },
                )

        elif self.state == VehicleState.MOVING:
            if speed < self.speed_threshold:
                self.state = VehicleState.IDLE
                self.idle_start_time = current_time

                logger.debug("Vehicle entered IDLE state")
                await event_callback(
                    "idle_start",
                    {
                        "trip_id": self.current_trip_id,
                        "lat": reading.latitude,
                        "lon": reading.longitude,
                    },
                )

        elif self.state == VehicleState.IDLE:
            if speed >= self.speed_threshold:
                self.state = VehicleState.MOVING
                self.idle_start_time = None

                logger.debug("Vehicle resumed MOVING")
                await event_callback(
                    "idle_end",
                    {
                        "trip_id": self.current_trip_id,
                    },
                )
            elif self.idle_start_time is not None:
                idle_duration = (current_time - self.idle_start_time).total_seconds()
                if idle_duration >= self.idle_timeout:
                    self.state = VehicleState.STOPPED
                    self.idle_start_time = None

                    logger.info(
                        "Ended trip: %s. Distance: %.2f km",
                        self.current_trip_id,
                        self.trip_distance_km,
                    )
                    await event_callback(
                        "trip_end",
                        {
                            "trip_id": self.current_trip_id,
                            "trip_distance_km": self.trip_distance_km,
                            "odometer_km": self.total_odometer_km,
                            "lat": reading.latitude,
                            "lon": reading.longitude,
                        },
                    )
                    self.current_trip_id = None
