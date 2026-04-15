#!/bin/sh
set -eu

# Pre-configure the GPS serial port: 9600 baud, raw mode, no echo.
# The BerryGPS-IMU V4 (u-blox M8) requires this before the Python reader opens
# the port, otherwise NMEA sentences may be garbled or the port may echo back.
stty -F "${GPS_SERIAL_DEVICE:-/dev/serial0}" 9600 raw -echo

exec python main.py
