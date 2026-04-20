# Use Balena's optimized Python base image
FROM balenalib/raspberrypi3-python:3.11-run

# Enable UDEV for hardware/Bluetooth access
ENV UDEV=1
# Route D-Bus to the host OS
ENV DBUS_SYSTEM_BUS_ADDRESS=unix:path=/host/run/dbus/system_bus_socket

# Set the working directory
WORKDIR /usr/src/app

# Copy dependency list and install
# build-essential is needed to compile uvloop (bundles libuv); purged in same
# layer so build tools don't remain in the final image.
COPY ble-sensor/requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy shared hardware library
COPY shared/ /usr/src/app/shared/

# Copy the rest of the application code
COPY ble-sensor/ .

# Execute the script
CMD ["python", "main.py"]
