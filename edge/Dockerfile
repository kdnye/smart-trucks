# Unified edge data-plane image: telematics-edge (GPS/IMU/heartbeats + cloud
# upload), ble-sensor (BLE scan), and sync-service (store-and-forward uploader)
# run as co-processes in one container (see edge/start.sh). Replaces the former
# separate telematics-edge / ble-sensor / sync-service / gps-multiplexer images.
FROM balenalib/raspberrypi3-python:3.11-run

ENV UDEV=1
# Route D-Bus to the host OS so bleak/BlueZ can scan (was set on ble-sensor).
ENV DBUS_SYSTEM_BUS_ADDRESS=unix:path=/host/run/dbus/system_bus_socket
# Service entrypoints live in per-service subdirs now, so put the repo root on
# PYTHONPATH for `import shared`. Each service's own dir is still sys.path[0]
# for its local imports (gps_filter / nmea_reader / sentry).
ENV PYTHONPATH=/usr/src/app

WORKDIR /usr/src/app

# build-essential compiles uvloop + aiohttp C extensions (no armv7 wheels) and is
# purged afterward to keep the image slim. i2c-tools + python3-smbus drive the
# IMU; coreutils provides stty for the GPS UART.
COPY edge/requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential i2c-tools python3-smbus coreutils \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY shared/ /usr/src/app/shared/
COPY telematics-edge/ /usr/src/app/telematics-edge/
COPY ble-sensor/ /usr/src/app/ble-sensor/
COPY sync-service/ /usr/src/app/sync-service/
COPY edge/start.sh /usr/src/app/start.sh
RUN chmod +x /usr/src/app/start.sh

CMD ["/bin/sh", "/usr/src/app/start.sh"]
