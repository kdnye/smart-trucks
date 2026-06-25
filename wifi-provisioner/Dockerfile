FROM balenalib/raspberrypi3-python:3.11-run

# network-manager provides the nmcli binary used to drive WiFi profiles, scans,
# and AP mode on the host. iproute2 is handy for diagnostics in `balena ssh`.
RUN apt-get update && apt-get install -yq \
    network-manager \
    iproute2 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

COPY wifi-provisioner/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ /usr/src/app/shared/
COPY wifi-provisioner/ .

CMD ["/bin/sh", "start.sh"]
