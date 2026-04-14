import asyncio
import logging
import socket as _socket
import threading
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
        """Dispatch to the TCP or serial reader depending on the configured port scheme."""
        if self.port.startswith("tcp://"):
            self._tcp_reader_loop(loop, stop_event)
        else:
            self._serial_reader_loop(loop, stop_event)

    def _serial_reader_loop(self, loop: asyncio.AbstractEventLoop, stop_event: threading.Event) -> None:
        """Blocking serial reader that forwards decoded lines into the asyncio queue."""
        reconnect_backoff_seconds = 1.0
        max_backoff_seconds = 30.0

        while not stop_event.is_set():
            try:
                with serial.Serial(self.port, baudrate=self.baudrate, timeout=1.0, exclusive=True) as conn:
                    logger.info("Successfully locked %s", self.port)
                    conn.reset_input_buffer()
                    reconnect_backoff_seconds = 3.0

                    while not stop_event.is_set():
                        try:
                            decoded_line = conn.readline().decode("ascii", errors="ignore").strip()
                            if decoded_line:
                                loop.call_soon_threadsafe(self._enqueue_line, decoded_line)
                        except serial.SerialException as exc:
                            logger.warning("Transient serial read error on %s: %s", self.port, exc)
                            break
            except serial.SerialException as exc:
                logger.error(
                    "Hardware port locked or unavailable on %s. Retrying in %.1fs (%s)",
                    self.port,
                    reconnect_backoff_seconds,
                    exc,
                )
                if stop_event.wait(reconnect_backoff_seconds):
                    break
                reconnect_backoff_seconds = min(max_backoff_seconds, reconnect_backoff_seconds * 2)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Fatal GPS reader error on %s. Retrying in %.1fs (%s)", self.port, reconnect_backoff_seconds, exc)
                if stop_event.wait(reconnect_backoff_seconds):
                    break
                reconnect_backoff_seconds = min(max_backoff_seconds, reconnect_backoff_seconds * 2)

    def _tcp_reader_loop(self, loop: asyncio.AbstractEventLoop, stop_event: threading.Event) -> None:
        """TCP client reader: connects to gps-multiplexer and forwards NMEA lines to the queue.

        Port format: tcp://host:port  (e.g. tcp://gps-multiplexer:2947)
        """
        addr = self.port[len("tcp://"):]
        host, port_str = addr.rsplit(":", 1)
        port = int(port_str)

        reconnect_backoff_seconds = 1.0
        max_backoff_seconds = 30.0

        while not stop_event.is_set():
            try:
                with _socket.create_connection((host, port), timeout=10) as sock:
                    logger.info("GPS TCP connected to %s:%d", host, port)
                    sock.settimeout(5.0)

                    data_received = False
                    buf = b""
                    while not stop_event.is_set():
                        try:
                            chunk = sock.recv(4096)
                        except _socket.timeout:
                            continue
                        if not chunk:
                            logger.warning("GPS TCP connection closed by multiplexer.")
                            break

                        if not data_received:
                            data_received = True
                            reconnect_backoff_seconds = 1.0

                        buf += chunk
                        while b"\n" in buf:
                            raw_line, buf = buf.split(b"\n", 1)
                            decoded_line = raw_line.decode("ascii", errors="ignore").strip()
                            if decoded_line:
                                loop.call_soon_threadsafe(self._enqueue_line, decoded_line)

            except (_socket.error, OSError) as exc:
                logger.warning(
                    "GPS TCP connection to %s:%d failed: %s. Reconnecting in %.1f seconds.",
                    host,
                    port,
                    exc,
                    reconnect_backoff_seconds,
                )
                if stop_event.wait(reconnect_backoff_seconds):
                    break
                reconnect_backoff_seconds = min(max_backoff_seconds, reconnect_backoff_seconds * 2)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error(
                    "GPS TCP unexpected error: %s. Reconnecting in %.1f seconds.",
                    exc,
                    reconnect_backoff_seconds,
                )
                if stop_event.wait(reconnect_backoff_seconds):
                    break
                reconnect_backoff_seconds = min(max_backoff_seconds, reconnect_backoff_seconds * 2)

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

        elif msg.sentence_type == "GGA":
            self.current_reading.fix_quality = int(getattr(msg, "gps_qual", 0) or 0)
            self.current_reading.satellites = int(getattr(msg, "num_sats", 0) or 0)
            self.current_reading.altitude = float(msg.altitude) if getattr(msg, "altitude", None) else None
            self.current_reading.hdop = float(msg.horizontal_dil) if getattr(msg, "horizontal_dil", None) else None

        elif msg.sentence_type == "GSA":
            self.current_reading.fix_type = int(getattr(msg, "mode_fix_type", 1) or 1)
            self.current_reading.pdop = float(msg.pdop) if getattr(msg, "pdop", None) else None

        elif msg.sentence_type == "VTG":
            self.current_reading.speed_kmh = (
                float(msg.spd_over_grnd_kmph) if getattr(msg, "spd_over_grnd_kmph", None) else None
            )
