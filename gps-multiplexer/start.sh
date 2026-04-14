#!/bin/sh
set -eu

SERIAL_DEVICE="${GPS_UPSTREAM_DEVICE:-/dev/serial0}"
VIRTUAL_DEVICE="${GPS_VIRTUAL_DEVICE:-/dev/virtual/gps_stream}"

mkdir -p "$(dirname "$VIRTUAL_DEVICE")"
rm -f "$VIRTUAL_DEVICE"

echo "gps-multiplexer starting: upstream=${SERIAL_DEVICE} virtual=${VIRTUAL_DEVICE}"

exec socat -d -d -u \
  "FILE:${SERIAL_DEVICE},ignoreeof" \
  "PTY,link=${VIRTUAL_DEVICE},rawer,echo=0,waitslave,mode=666"
