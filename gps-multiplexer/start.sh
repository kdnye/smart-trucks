#!/bin/sh
set -eu

SERIAL_DEVICE="${GPS_SERIAL_DEVICE:-/dev/serial0}"

echo "gps-multiplexer starting: serial=${SERIAL_DEVICE}"

if printf "%s" "${SERIAL_DEVICE}" | grep -Eq '^[Tt][Cc][Pp]://'; then
  echo "GPS_SERIAL_DEVICE is TCP - skipping stty"
else
  if [ ! -c "${SERIAL_DEVICE}" ]; then
    echo "GPS serial device ${SERIAL_DEVICE} is missing or not a character device"
    exit 1
  fi

  # Force the UART to the expected module configuration before Python opens it.
  stty -F "${SERIAL_DEVICE}" 9600 raw -echo
fi

exec python main.py
