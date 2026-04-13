import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Optional

import pynmea2
import serial

logger = logging.getLogger(__name__)


@dataclass
class GpsReading:
    """Structured representation of the latest GPS state across NMEA sentence types."""

    timestamp: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[float] = None
    speed_kmh: Optional[float] = None
    heading: Optional[float] = None
    fix_quality: int = 0
    satellites: int = 0
    hdop: Optional[float] = None
    pdop: Optional[float] = None
    fix_type: int = 1  # 1 = no fix, 2 = 2D, 3 = 3D


class NMEAReader:
    """Async NMEA reader with reconnect support and merged GPS state updates."""

    def __init__(self, port: str = "/dev/serial0", baudrate: int = 9600) -> None:
        self.port = port
        self.baudrate = baudrate
        self.serial_conn: Optional[serial.Serial] = None
        self.current_reading = GpsReading()

    def connect(self) -> bool:
        """Attempt to open the serial port."""
        try:
            self.serial_conn = serial.Serial(self.port, self.baudrate, timeout=1)
            logger.info("Connected to GPS on %s at %s baud", self.port, self.baudrate)
            return True
        except serial.SerialException as exc:
            logger.error("GPS serial error on %s: %s", self.port, exc)
            return False

    async def read_loop(self, callback: Callable[[GpsReading], Awaitable[None]]) -> None:
        """Continuously read and parse NMEA lines, invoking callback on each RMC tick."""
        while True:
            if not self.serial_conn or not self.serial_conn.is_open:
                if not self.connect():
                    await asyncio.sleep(5)
                    continue

            try:
                line = await asyncio.to_thread(self.serial_conn.readline)
                if not line:
                    continue

                decoded_line = line.decode("ascii", errors="ignore").strip()
                if not decoded_line.startswith("$"):
                    continue

                msg = pynmea2.parse(decoded_line)
                self._update_reading(msg)

                if msg.sentence_type == "RMC":
                    await callback(self.current_reading)

            except pynmea2.ParseError as exc:
                logger.debug("Malformed NMEA sentence ignored: %s", exc)
            except serial.SerialException as exc:
                logger.error("Serial connection lost: %s", exc)
                if self.serial_conn:
                    self.serial_conn.close()
                self.serial_conn = None
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Unexpected error in NMEA read loop: %s", exc)
                await asyncio.sleep(1)

    def _update_reading(self, msg: pynmea2.NMEASentence) -> None:
        """Update shared GPS state using only known sentence types."""
        if msg.sentence_type == "RMC":
            if getattr(msg, "status", "") == "A":
                self.current_reading.timestamp = getattr(msg, "datetime", None)
                self.current_reading.latitude = msg.latitude
                self.current_reading.longitude = msg.longitude
                self.current_reading.heading = getattr(msg, "true_course", None)

        elif msg.sentence_type == "GGA":
            self.current_reading.fix_quality = int(getattr(msg, "gps_qual", 0) or 0)
            self.current_reading.satellites = int(getattr(msg, "num_sats", 0) or 0)
            self.current_reading.altitude = float(msg.altitude) if getattr(msg, "altitude", None) else None
            self.current_reading.hdop = float(msg.horizontal_dil) if getattr(msg, "horizontal_dil", None) else None

        elif msg.sentence_type == "GSA":
            self.current_reading.fix_type = int(getattr(msg, "mode_fix_type", 1) or 1)
            self.current_reading.pdop = float(msg.pdop) if getattr(msg, "pdop", None) else None

        elif msg.sentence_type == "VTG":
            self.current_reading.speed_kmh = float(msg.spd_over_grnd_kmph) if getattr(msg, "spd_over_grnd_kmph", None) else None
