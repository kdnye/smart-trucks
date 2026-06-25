FROM balenalib/raspberrypi3-python:3.11-run

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
