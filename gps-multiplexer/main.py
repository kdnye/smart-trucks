#!/usr/bin/env python3
"""
GPS serial multiplexer.

Reads raw NMEA sentences from the hardware UART and broadcasts each line to
every connected TCP client on GPS_TCP_PORT (default 2947).

This is the *sole* owner of /dev/serial0.  All other containers receive GPS
data by connecting to this service over the Docker network — they never touch
the physical serial port directly.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading

import serial
import uvloop

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

GPS_PORT = os.getenv("GPS_SERIAL_DEVICE", "/dev/serial0")
GPS_BAUD = int(os.getenv("GPS_BAUD_RATE", "9600"))
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.getenv("GPS_TCP_PORT", "2947"))


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

        while not stop_event.is_set():
            try:
                # exclusive=True (TIOCEXCL) prevents any other process from opening
                # the port while we hold it.
                with serial.Serial(GPS_PORT, GPS_BAUD, timeout=2, exclusive=True) as conn:
                    logger.info("GPS serial connected: port=%s baud=%d", GPS_PORT, GPS_BAUD)
                    backoff = 1.0

                    while not stop_event.is_set():
                        line = conn.readline()
                        if line:
                            self.broadcast_from_thread(line)

            except serial.SerialException as exc:
                logger.warning(
                    "GPS serial error: %s. Reconnecting in %.1fs.", exc, backoff
                )
                stop_event.wait(backoff)
                backoff = min(max_backoff, backoff * 2)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error(
                    "GPS serial unexpected error: %s. Reconnecting in %.1fs.", exc, backoff
                )
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
        try:
            # Block here until the client closes the connection (EOF).
            # GPS clients are receive-only, so we never expect inbound data.
            while True:
                chunk = await reader.read(256)
                if not chunk:
                    break
        finally:
            broadcaster.remove_client(writer)
            with contextlib.suppress(Exception):
                writer.close()

    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    logger.info("GPS multiplexer listening on %s", addrs)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    uvloop.install()
    asyncio.run(run())
