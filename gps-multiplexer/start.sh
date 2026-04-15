#!/bin/sh
set -eu

SERIAL_DEVICE="${GPS_SERIAL_DEVICE:-/dev/serial0}"
SERIAL_CANDIDATES="${GPS_SERIAL_CANDIDATES:-/dev/serial0,/dev/ttyAMA0,/dev/ttyS0}"

echo "gps-multiplexer starting: serial=${SERIAL_DEVICE} candidates=${SERIAL_CANDIDATES}"

# Pre-configure the first available GPS serial port: 9600 baud, raw mode, no echo.
# Non-fatal because container hardware mappings vary by Pi model.
if printf "%s" "${SERIAL_DEVICE}" | grep -Eq '^[Tt][Cc][Pp]://'; then
  echo "GPS_SERIAL_DEVICE is TCP — skipping stty"
else
  STTY_TARGET=""
  IFS=','
  for candidate in ${SERIAL_CANDIDATES}; do
    candidate_trimmed="$(echo "${candidate}" | xargs)"
    if [ -c "${candidate_trimmed}" ]; then
      STTY_TARGET="${candidate_trimmed}"
      break
    fi
  done
  unset IFS

  if [ -z "${STTY_TARGET}" ] && [ -c "${SERIAL_DEVICE}" ]; then
    STTY_TARGET="${SERIAL_DEVICE}"
  fi

  if [ -n "${STTY_TARGET}" ]; then
    stty -F "${STTY_TARGET}" 9600 raw -echo || echo "Warning: stty failed for ${STTY_TARGET} — continuing"
  else
    echo "Warning: no character-device GPS serial candidates found for stty — continuing"
  fi
fi

exec python main.py
