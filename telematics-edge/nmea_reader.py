import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Optional

import pynmea2

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
        self.device = port
        self.baud = baudrate
        self.current_reading = GpsReading()
        self._line_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._reader_stop_event: Optional[threading.Event] = None
        self._reader_thread: Optional[threading.Thread] = None

    async def read_loop(self, callback: Callable[[GpsReading], Awaitable[None]]) -> None:
        """Continuously read and parse NMEA lines, invoking callback on each RMC tick."""
        self._start_reader_thread(asyncio.get_running_loop())

        try:
            while True:
                decoded_line = await self._line_queue.get()
                if not decoded_line.startswith("$"):
                    continue

                try:
                    msg = pynmea2.parse(decoded_line)
                except pynmea2.ParseError as exc:
                    logger.debug("Malformed NMEA sentence ignored: %s", exc)
                    continue

                self._update_reading(msg)

                if msg.sentence_type == "RMC":
                    await callback(self.current_reading)
        except asyncio.CancelledError:
            logger.info("NMEA reader loop cancelled")
            raise
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Unexpected error in NMEA read loop: %s", exc)
            await asyncio.sleep(1)
        finally:
            self._stop_reader_thread()

    def _start_reader_thread(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start a fresh reader thread for the current serial connection."""
        self._stop_reader_thread()
        self._reader_stop_event = threading.Event()
        self._reader_thread = threading.Thread(
            target=self._serial_reader_thread,
            args=(loop, self._reader_stop_event),
            daemon=True,
            name="gps-serial-reader",
        )
        self._reader_thread.start()

    def _stop_reader_thread(self) -> None:
        """Stop and clear the reader thread state, releasing serial resources."""
        if self._reader_stop_event:
            self._reader_stop_event.set()

        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=3.0)
            if self._reader_thread.is_alive():
                logger.warning("GPS reader thread did not stop cleanly within 3.0s")

        self._reader_stop_event = None
        self._reader_thread = None

    def _serial_reader_thread(self, loop: asyncio.AbstractEventLoop, stop_event: threading.Event) -> None:
        """Blocking serial reader that forwards decoded lines into the asyncio queue."""
        self._serial_reader_loop(loop, stop_event)

    def _serial_reader_loop(self, loop: asyncio.AbstractEventLoop, stop_event: threading.Event) -> None:
        """Blocking serial reader that forwards decoded lines into the asyncio queue."""
        import serial

        reconnect_backoff_seconds = 1.0
        max_backoff_seconds = 30.0

        while not stop_event.is_set():
            try:
                with serial.Serial(
                    self.port,
                    baudrate=self.baudrate,
                    timeout=1.0,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                ) as conn:
                    logger.info("Successfully connected to %s", self.port)
                    conn.reset_input_buffer()
                    reconnect_backoff_seconds = 3.0

                    while not stop_event.is_set():
                        try:
                            raw_line = conn.readline()
                            if not raw_line:
                                continue

                            decoded_line = raw_line.decode("ascii", errors="ignore").strip()
                            if decoded_line:
                                loop.call_soon_threadsafe(self._enqueue_line, decoded_line)
                        except serial.SerialException as exc:
                            logger.warning("Transient serial read error on %s: %s", self.port, exc)
                            break
            except serial.SerialException as exc:
                logger.error(
                    "Hardware port unavailable on %s. Retrying in %.1fs (%s)",
                    self.port, reconnect_backoff_seconds, exc
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Fatal GPS reader error. Retrying in %.1fs (%s)", reconnect_backoff_seconds, exc)

            if stop_event.wait(reconnect_backoff_seconds):
                break
            reconnect_backoff_seconds = min(reconnect_backoff_seconds * 2.0, max_backoff_seconds)

    def _tcp_reader_loop(self, loop: asyncio.AbstractEventLoop, stop_event: threading.Event) -> None:
        """Legacy TCP mode removed; GPS now uses direct serial hardware access."""
        _ = loop
        _ = stop_event
        logger.error("TCP GPS mode is no longer supported; configure a serial device path.")

    def _enqueue_line(self, line: str) -> None:
        """Enqueue latest NMEA line without blocking event loop."""
        if self._line_queue.full():
            try:
                _ = self._line_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass

        try:
            self._line_queue.put_nowait(line)
        except asyncio.QueueFull:
            logger.debug("Dropping GPS sentence due to full queue")

    def _update_reading(self, msg: pynmea2.NMEASentence) -> None:
        """Update shared GPS state using only known sentence types."""
        if msg.sentence_type == "RMC":
            if getattr(msg, "status", "") == "A":
                self.current_reading.timestamp = getattr(msg, "datetime", None)
                self.current_reading.latitude = msg.latitude
                self.current_reading.longitude = msg.longitude
                self.current_reading.heading = getattr(msg, "true_course", None)
                # RMC status "A" means the fix is valid. Promote fix_type to at
                # least 2 (2D) so _is_valid_fix passes even when the GPS module
                # hasn't sent a GSA sentence yet (or doesn't send one at all).
                if self.current_reading.fix_type < 2:
                    self.current_reading.fix_type = 2
                # Capture speed from RMC as a fallback when VTG is absent.
                if self.current_reading.speed_kmh is None:
                    spd_knots = getattr(msg, "spd_over_grnd", None)
                    if spd_knots is not None:
                        try:
                            self.current_reading.speed_kmh = float(spd_knots) * 1.852
                        except (ValueError, TypeError):
                            pass
            else:
                # RMC status "V" (void) means the fix was lost; clear stale
                # position data so _is_valid_fix rejects this reading.
                self.current_reading.latitude = None
                self.current_reading.longitude = None
                self.current_reading.fix_type = 1

        elif msg.sentence_type == "GGA":
            self.current_reading.fix_quality = int(getattr(msg, "gps_qual", 0) or 0)
            self.current_reading.satellites = int(getattr(msg, "num_sats", 0) or 0)
            # Keep position fresh from GGA as well. This provides a resilient
            # fallback when some RMC lines are malformed or intermittently
            # missing on noisy serial links.
            if getattr(msg, "latitude", None) is not None:
                self.current_reading.latitude = msg.latitude
            if getattr(msg, "longitude", None) is not None:
                self.current_reading.longitude = msg.longitude
            self.current_reading.altitude = float(msg.altitude) if getattr(msg, "altitude", None) else None
            self.current_reading.hdop = float(msg.horizontal_dil) if getattr(msg, "horizontal_dil", None) else None

        elif msg.sentence_type == "GSA":
            self.current_reading.fix_type = int(getattr(msg, "mode_fix_type", 1) or 1)
            self.current_reading.pdop = float(msg.pdop) if getattr(msg, "pdop", None) else None

        elif msg.sentence_type == "GLL":
            # GLL carries latitude/longitude + validity status and can arrive
            # even when RMC is absent.
            if getattr(msg, "status", "") == "A":
                if getattr(msg, "latitude", None) is not None:
                    self.current_reading.latitude = msg.latitude
                if getattr(msg, "longitude", None) is not None:
                    self.current_reading.longitude = msg.longitude
                if self.current_reading.fix_type < 2:
                    self.current_reading.fix_type = 2

        elif msg.sentence_type == "VTG":
            try:
                self.current_reading.speed_kmh = (
                    float(msg.spd_over_grnd_kmph) if getattr(msg, "spd_over_grnd_kmph", None) else None
                )
            except (ValueError, TypeError):
                self.current_reading.speed_kmh = None
