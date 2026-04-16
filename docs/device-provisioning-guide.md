# 🚛 Smart Truck Telematics: Device Provisioning & Dashboard Sync Guide

This guide outlines the end-to-end process for bringing a brand new Raspberry Pi Zero 2 W (with BerryGPS-IMU and UPS HAT) online, configuring its hardware ports via Balena, assigning its digital identity, and verifying its connection to the Google Cloud dashboard.

## Phase 1: Hardware Configuration (Balena)
When a new Raspberry Pi is flashed and added to the Balena fleet, its hardware pins (Serial and I2C) are turned off by default to save power. You must enable them so the Pi can talk to the GPS and power monitor.

1. Log into **balenaCloud** and select your new device (e.g., `Truck_Test`).
2. On the left menu, click **Device Configuration**.
3. Scroll down to **Custom Configuration Variables** and add/modify the following:
   * **Name:** `RESIN_HOST_CONFIG_enable_uart` | **Value:** `1` *(Turns on the serial pins)*
   * **Name:** `RESIN_HOST_CONFIG_dtparam` | **Value:** `i2c_arm=on,spi=on` *(Turns on the I2C bus for the battery monitor. **Do not use quotes**).*
4. Scroll to **DT overlays** and add `miniuart-bt`. *(This maps the high-performance hardware serial port to the GPS chip instead of Bluetooth).*

5. The device will automatically reboot to apply these hardware changes.

> UART must be enabled at the **host OS** level (`enable_uart=1`, typically via `RESIN_HOST_CONFIG_enable_uart=1` in Balena). If UART is not enabled, GPS serial devices will not be exposed on the host.

### Serial Device Mapping for GPS Auto-Discovery
The `gps-multiplexer` container supports GPS serial auto-discovery using `GPS_SERIAL_CANDIDATES`. For this to work, Docker/Balena must map **every candidate host UART node** into the container under `devices:`.

Current expected candidate set:
- `/dev/serial0`
- `/dev/ttyAMA0`
- `/dev/ttyS0`

If any candidate path is missing from container device mappings, auto-discovery may fail on boards/boot modes that expose GPS on a different UART alias.

## Phase 2: Software Identity (Device Variables)
Next, the Python containers need to know *where* to send their data and *who* they are.

1. On the left menu, click **Device Variables** *(Make sure you are in Variables, not Configuration).*
2. Add the following required routing variables:
   * **Name:** `WEBHOOK_URL` | **Value:** `https://us-central1-.../motiveWebhookReceiver` *(Your GCP endpoint)*
   * **Name:** `API_KEY` | **Value:** `[Your API Key]`
   * **Name:** `POLL_INTERVAL` | **Value:** `30`
3. Add the **CRITICAL** identity variable. Without this, the system will name the truck `UNKNOWN_TRUCK` and hide it from your dashboard:
   * **Name:** `VEHICLE_ID` | **Value:** `[Name of Truck]` *(e.g., `Truck_Test`, `TRK-104`)*

> `VEHICLE_ID` is the required identity variable for this stack. Do **not** use `MOTIVE_TRUCK_NUMBER` as a substitute.

*(Note: If you want all trucks to share the same Webhook URL and API keys, you can set those at the **Fleet Variables** level instead, so you only have to set the `VEHICLE_ID` for new trucks).*

## Phase 3: The "Cold Start" (GPS Lock)
A brand-new GPS chip has no idea where it is in the world. It must download a satellite "Almanac" from space before it can lock on.

1. Ensure the Raspberry Pi is powered on.
2. Take the unit **outside** or place it on the dashboard of a vehicle with a completely unobstructed view of the sky. (Metal warehouse roofs will block the signal entirely).
3. Leave it running for **10 to 15 minutes**.
4. You can monitor the Balena Logs for the `telematics-edge` container. It will initially say `Status: searching`. Once it downloads the almanac, you will see raw NMEA coordinates start flowing.

## Phase 4: Dashboard Verification
Once the device has its `VEHICLE_ID` and an active internet connection, it will begin firing payloads to Google Cloud.

1. Open your Streamlit Dashboard.
2. The new `VEHICLE_ID` will automatically appear in the **Select Raspberry Pi** dropdown menu dynamically based on incoming heartbeats.
3. Select the truck to view its Live Location, BLE asset inventory, and Power State.
