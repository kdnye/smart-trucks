#!/bin/sh
set -eu

SERIAL_DEVICE="${GPS_UPSTREAM_DEVICE:-/dev/serial0}"
VIRTUAL_DEVICE="${GPS_VIRTUAL_DEVICE:-/dev/virtual/gps_stream}"

mkdir -p "$(dirname "$VIRTUAL_DEVICE")"
rm -f "$VIRTUAL_DEVICE"

echo "gps-multiplexer starting: upstream=${SERIAL_DEVICE} virtual=${VIRTUAL_DEVICE}"

# Pre-configure the GPS serial port: 9600 baud, raw mode, no echo.
# Required for the BerryGPS-IMU V4 (u-blox M8) to stream NMEA sentences cleanly.
# Non-fatal: if the device node isn't visible yet (e.g. symlink not resolved
# in this container), socat will produce a clearer error and we avoid a
# silent restart loop caused by set -eu.
stty -F "${SERIAL_DEVICE}" 9600 raw -echo || echo "Warning: stty failed for ${SERIAL_DEVICE} — continuing"

exec socat -d -d -u \
  "FILE:${SERIAL_DEVICE},ignoreeof" \
  "PTY,link=${VIRTUAL_DEVICE},rawer,echo=0,waitslave,mode=666"
