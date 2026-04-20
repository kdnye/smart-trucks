# Use Balena's optimized Python base image
FROM balenalib/raspberrypi3-python:3.11-run

# Enable UDEV for hardware/Bluetooth access
ENV UDEV=1
# Route D-Bus to the host OS
ENV DBUS_SYSTEM_BUS_ADDRESS=unix:path=/host/run/dbus/system_bus_socket

# Set the working directory
WORKDIR /usr/src/app

# Install C build tools required by uvloop (compiles libuv from source)
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency list and install
COPY ble-sensor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared hardware library
COPY shared/ /usr/src/app/shared/

# Copy the rest of the application code
COPY ble-sensor/ .

# Execute the script
CMD ["python", "main.py"]
