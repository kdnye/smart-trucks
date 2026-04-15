#!/bin/sh
set -eu

# Pre-configure the GPS serial port: 9600 baud, raw mode, no echo.
# The BerryGPS-IMU V4 (u-blox M8) requires this before the Python reader opens
# the port, otherwise NMEA sentences may be garbled or the port may echo back.
# Non-fatal: if the symlink isn't resolved in this container the Python reader
# will still attempt the serial open and log a clearer error.
# Only configure the serial port when GPS_SERIAL_DEVICE is a local device path.
# TCP URLs (e.g. tcp://gps-multiplexer:2947) are not serial ports and stty would
# just print a confusing error and waste time.
case "${GPS_SERIAL_DEVICE:-/dev/serial0}" in
  tcp://*)
    echo "GPS_SERIAL_DEVICE is TCP — skipping stty"
    ;;
  *)
    stty -F "${GPS_SERIAL_DEVICE:-/dev/serial0}" 9600 raw -echo \
      || echo "Warning: stty failed for ${GPS_SERIAL_DEVICE:-/dev/serial0} — continuing"
    ;;
esac

exec python main.py
