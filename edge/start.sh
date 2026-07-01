#!/bin/sh
# Edge data-plane supervisor. Runs the three edge services as co-processes in one
# container, each restarted if it exits:
#   - telematics-edge : GPS + IMU + heartbeats/health producer + cloud upload loop
#   - ble-sensor      : BLE scan -> enqueue ble_scans in the shared SQLite DB
#   - sync-service    : drain the shared SQLite queues -> cloud
# Coarse, container-level restart-on-wedge is handled by the wifi-provisioner
# watchdog (it restarts this whole `edge` service via the Balena Supervisor API
# when heartbeats go stale).
set -u

GPS_SERIAL_DEVICE="${GPS_SERIAL_DEVICE:-/dev/serial0}"

# Pre-configure the GPS UART (9600 raw, no echo) before the reader opens it.
# Skip for tcp:// sources; tolerate a missing device (logged, non-fatal).
case "${GPS_SERIAL_DEVICE}" in
  tcp://*)
    echo "[edge] GPS_SERIAL_DEVICE is TCP (${GPS_SERIAL_DEVICE}) — skipping stty"
    ;;
  *)
    if [ -c "${GPS_SERIAL_DEVICE}" ]; then
      stty -F "${GPS_SERIAL_DEVICE}" 9600 raw -echo \
        || echo "[edge] warning: stty failed for ${GPS_SERIAL_DEVICE} — continuing"
    else
      echo "[edge] warning: GPS serial device ${GPS_SERIAL_DEVICE} unavailable — continuing"
    fi
    ;;
esac

supervise() {
  name="$1"
  shift
  (
    while true; do
      echo "[edge] starting ${name}"
      "$@"
      code=$?
      echo "[edge] ${name} exited (${code}); restarting in 5s"
      sleep 5
    done
  ) &
}

shutdown() {
  echo "[edge] received signal — stopping co-processes"
  # Reset the trap first, then signal the whole process group so the Python
  # children (not just the supervise subshells) get a graceful SIGTERM to flush
  # SQLite / WAL before the container tears down.
  trap - TERM INT
  kill 0 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

supervise telematics-edge python /usr/src/app/telematics-edge/main.py
supervise ble-sensor      python /usr/src/app/ble-sensor/main.py
supervise sync-service    python /usr/src/app/sync-service/main.py

# ATOM Lite anchor pipeline (opt-out with ANCHOR_PIPELINE_ENABLED=false):
# mosquitto is the loopback-only broker (edge/mosquitto.conf); anchor-bridge
# pumps the gateway ATOM's USB serial NDJSON into it; ble-sensor's in-process
# anchor ingest subscribes. anchor-bridge idles cheaply (30s probes) when no
# gateway is plugged in, so sites without anchors pay ~zero cost.
if [ "${ANCHOR_PIPELINE_ENABLED:-true}" = "true" ]; then
  supervise mosquitto     mosquitto -c /usr/src/app/edge/mosquitto.conf
  supervise anchor-bridge python /usr/src/app/anchor-bridge/main.py
fi

wait
