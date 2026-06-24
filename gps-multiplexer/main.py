#!/usr/bin/env python3
"""
GPS serial multiplexer.

Reads raw NMEA sentences from the hardware UART and broadcasts each line to
every connected TCP client on GPS_TCP_PORT (default 2947).

This is the *sole* owner of the Pi UART character device(s). All other
containers receive GPS data by connecting to this service over the Docker
network — they never touch the physical serial port directly.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
import threading

import serial
import uvloop

from shared.env import read_int_env

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

GPS_BAUD = read_int_env("GPS_BAUD_RATE", 9600)
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = read_int_env("GPS_TCP_PORT", 2947)
DEFAULT_GPS_PORT = "/dev/serial0"
DEFAULT_GPS_CANDIDATES = (
    # Pi GPIO UART (preferred for BerryGPS-IMU and other HAT modules).
    # On Pi Zero 2 W / Pi 3+ this requires `enable_uart=1` AND `dtoverlay=disable-bt`
    # in the Balena host config, or `serial0` falls back to the unstable mini-UART.
    "/dev/serial0",
    "/dev/ttyAMA0",
    "/dev/ttyS0",
    # USB GPS receivers (u-blox, GlobalSat, etc.). Enumerated by the kernel only
    # when a USB GPS is plugged in; harmless to probe when absent.
    "/dev/ttyACM0",
    "/dev/ttyUSB0",
)


def _parse_candidates() -> tuple[str, ...]:
    configured_port = os.getenv("GPS_SERIAL_DEVICE", DEFAULT_GPS_PORT).strip()
    configured_candidates = os.getenv("GPS_SERIAL_CANDIDATES", "").strip()

    candidates: list[str] = []

    def add_candidate(candidate: str) -> None:
        value = candidate.strip()
        if value and value not in candidates:
            candidates.append(value)

    if configured_port and not configured_port.lower().startswith("tcp://"):
        add_candidate(configured_port)

    if configured_candidates:
        for candidate in configured_candidates.split(","):
            add_candidate(candidate)

    for candidate in DEFAULT_GPS_CANDIDATES:
        add_candidate(candidate)

    return tuple(candidates)


GPS_PORT_CANDIDATES = _parse_candidates()


class GPSBroadcaster:
    """Thread-safe broadcaster: one serial reader thread → many asyncio TCP writers."""

    def __init__(self) -> None:
        self._clients: set[asyncio.StreamWriter] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ── client management ────────────────────────────────────────────────────

    def add_client(self, writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        addr = writer.get_extra_info("peername", "?")
        logger.info("GPS client connected: %s  active=%d", addr, len(self._clients))

    def remove_client(self, writer: asyncio.StreamWriter) -> None:
        self._clients.discard(writer)
        addr = writer.get_extra_info("peername", "?")
        logger.info("GPS client disconnected: %s  active=%d", addr, len(self._clients))

    # ── broadcast ────────────────────────────────────────────────────────────

    def broadcast_from_thread(self, line: bytes) -> None:
        """Called from the serial reader thread; dispatches into the asyncio event loop."""
        if self._loop is not None and self._clients:
            asyncio.run_coroutine_threadsafe(self._broadcast(line), self._loop)

    async def _broadcast(self, line: bytes) -> None:
        dead: set[asyncio.StreamWriter] = set()
        for writer in list(self._clients):
            try:
                writer.write(line)
                await asyncio.wait_for(writer.drain(), timeout=2.0)
            except Exception:  # pylint: disable=broad-except
                dead.add(writer)
        for writer in dead:
            self._clients.discard(writer)
            with contextlib.suppress(Exception):
                writer.close()

    # ── serial reader (daemon thread) ────────────────────────────────────────

    def run_serial_reader(self, stop_event: threading.Event) -> None:
        """Blocking loop: open /dev/serial0 exclusively, read lines, broadcast."""
        backoff = 1.0
        max_backoff = 30.0

        logger.info("GPS serial candidates: %s", ", ".join(GPS_PORT_CANDIDATES))

        while not stop_event.is_set():
            serial_connected = False
            probe_results: list[str] = []

            for port in GPS_PORT_CANDIDATES:
                port_path = pathlib.Path(port)
                if not port_path.exists():
                    probe_results.append(f"{port}=missing")
                    continue
                if not port_path.is_char_device():
                    probe_results.append(f"{port}=not-a-char-device")
                    logger.warning("Skipping non-character GPS device candidate: %s", port)
                    continue

                try:
                    # exclusive=True (TIOCEXCL) prevents any other process from opening
                    # the port while we hold it.
                    with serial.Serial(port, GPS_BAUD, timeout=2, exclusive=True) as conn:
                        logger.info("GPS serial connected: port=%s baud=%d", port, GPS_BAUD)
                        backoff = 1.0
                        serial_connected = True

                        while not stop_event.is_set():
                            line = conn.readline()
                            if line:
                                self.broadcast_from_thread(line)
                        break
                except serial.SerialException as exc:
                    probe_results.append(f"{port}=open-error({exc})")
                    logger.warning("GPS serial error on %s: %s", port, exc)
                except Exception as exc:  # pylint: disable=broad-except
                    probe_results.append(f"{port}=unexpected-error({exc})")
                    logger.error("GPS serial unexpected error on %s: %s", port, exc)

            if stop_event.is_set():
                break
            if not serial_connected:
                logger.warning(
                    "SETUP: no usable GPS serial device. Per-candidate probe: [%s]. "
                    "If every candidate is `missing`, the Pi kernel has not exposed any "
                    "serial device — for a GPIO/HAT GPS (BerryGPS-IMU etc.) set both "
                    "RESIN_HOST_CONFIG_enable_uart=1 AND "
                    "RESIN_HOST_CONFIG_dtoverlay=disable-bt in Balena Device "
                    "Configuration and reboot (disable-bt is required on Pi Zero 2 W / "
                    "Pi 3+ to free /dev/ttyAMA0 from Bluetooth). For a USB GPS, confirm "
                    "the receiver is plugged in and check `ls /dev/tty*` on the host. "
                    "Heartbeats still upload, but without lat/lon until GPS is found.",
                    ", ".join(probe_results) or "no candidates configured",
                )

            logger.warning("Reconnecting in %.1fs.", backoff)
            stop_event.wait(backoff)
            backoff = min(max_backoff, backoff * 2)


async def run() -> None:
    broadcaster = GPSBroadcaster()
    broadcaster.set_loop(asyncio.get_running_loop())

    stop_event = threading.Event()
    threading.Thread(
        target=broadcaster.run_serial_reader,
        args=(stop_event,),
        daemon=True,
        name="gps-serial-reader",
    ).start()

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        broadcaster.add_client(writer)
        addr = writer.get_extra_info("peername", "?")
        try:
            # Block here until the client closes the connection (EOF).
            # GPS clients are receive-only, so we never expect inbound data.
            while True:
                chunk = await reader.read(256)
                if not chunk:
                    break
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError) as exc:
            logger.info("GPS client abruptly disconnected: %s error=%s", addr, exc)
        finally:
            broadcaster.remove_client(writer)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    logger.info("GPS multiplexer listening on %s", addrs)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    uvloop.install()
    asyncio.run(run())
