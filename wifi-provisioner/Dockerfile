FROM balenalib/raspberrypi3-python:3.11-run

# Route D-Bus to the host OS so nmcli/libnm can reach the host NetworkManager.
# Without this, nmcli fails with "Could not create NMClient object: Could not
# connect: No such file or directory" and the service can never raise the setup
# AP or manage WiFi (the io.balena.features.dbus label mounts the socket here but
# does not set this address). Mirrors edge/ble-sensor.
ENV DBUS_SYSTEM_BUS_ADDRESS=unix:path=/host/run/dbus/system_bus_socket

# network-manager provides the nmcli binary used to drive WiFi profiles, scans,
# and AP mode on the host. iproute2 is handy for diagnostics in `balena ssh`.
RUN apt-get update && apt-get install -yq \
    network-manager \
    iproute2 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

# build-essential supplies gcc, needed to compile aiohttp's C extensions since
# no prebuilt armv7l wheel is published; purged in the same layer so the build
# tools don't bloat the final image.
COPY wifi-provisioner/requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY shared/ /usr/src/app/shared/
COPY wifi-provisioner/ .

CMD ["/bin/sh", "start.sh"]
