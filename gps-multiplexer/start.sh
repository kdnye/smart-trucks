#!/bin/sh
set -eu

SERIAL_DEVICE="${GPS_SERIAL_DEVICE:-/dev/serial0}"

echo "gps-multiplexer starting: serial=${SERIAL_DEVICE}"

# Pre-configure the GPS serial port: 9600 baud, raw mode, no echo.
# Required for the BerryGPS-IMU V4 (u-blox M8) to stream NMEA sentences cleanly.
# Non-fatal: /dev/serial0 is a symlink on the Pi and Docker does not always
# resolve it before Python opens the port; the Python reader will log a clear
# error if the device is genuinely absent.
stty -F "${SERIAL_DEVICE}" 9600 raw -echo || echo "Warning: stty failed for ${SERIAL_DEVICE} — continuing"

exec python main.py
