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

### gps-multiplexer startup behavior (important)
`gps-multiplexer/start.sh` does **not** hard-exit when `GPS_SERIAL_DEVICE` (default `/dev/serial0`) is missing. It logs a warning and continues launching Python so runtime auto-discovery can still find another mapped UART candidate.

`gps-multiplexer/start.sh` **does** hard-exit when the app entrypoint is missing (`/usr/src/app/main.py`), because the service cannot start without executable application code.

Troubleshooting note: serial discovery and reconnect logic run in `gps-multiplexer/main.py`, where candidates are built from `GPS_SERIAL_CANDIDATES` (plus defaults such as `/dev/serial0`, `/dev/ttyAMA0`, and `/dev/ttyS0`).

## Phase 2: Software Identity (Device Variables)
Next, the Python containers need to know *where* to send their data and *who* they are.

1. On the left menu, click **Device Variables** *(Make sure you are in Variables, not Configuration).*
2. Add the following required routing variables:
   * **Name:** `WEBHOOK_URL` | **Value:** `https://us-central1-.../motiveWebhookReceiver` *(Your GCP endpoint)*
   * **Name:** `API_KEY` | **Value:** `[Your API Key]`
   * **Name:** `POLL_INTERVAL` | **Value:** `30`
3. Add the **CRITICAL** identity variable. Without this, the system will name the truck `UNKNOWN_TRUCK` and hide it from your dashboard:
   * **Name:** `VEHICLE_ID` | **Value:** `[Name of Truck]` *(e.g., `Truck_Test`, `TRK-104`)*
4. Add UPS calibration variables for `power-monitor` so INA219 readings are scaled correctly for your UPS hardware profile:
   * **Name:** `UPS_MAX_EXPECTED_AMPS` | **Value:** Use the profile table below.
   * **Name:** `UPS_GAIN_STRATEGY` | **Value:** `auto` *(recommended; lets INA219 pick gain from expected amps).*
   * **Name:** `UPS_BUS_VOLTAGE_RANGE_V` | **Value:** `32` *(default for 5V UPS rails with transient headroom).*

> **Devices with no UPS HAT:** Some devices have no INA219-based UPS — for example a solar/MPPT charge board that supplies 5V but exposes no battery telemetry over I2C. For those devices, set **Name:** `UPS` | **Value:** `no`. `power-monitor` then skips the I2C UPS probe and idles instead of crash-looping with `Hardware probe failed: Required UPS device missing`. Leave `UPS` unset (or `yes`) on devices that do have a UPS HAT. Accepted "no UPS" values: `no`, `n`, `false`, `0`, `off`, `none`, `absent`, `disabled`.

> `VEHICLE_ID` is the required identity variable for this stack. Do **not** use `MOTIVE_TRUCK_NUMBER` as a substitute.

*(Note: If you want all trucks to share the same Webhook URL and API keys, you can set those at the **Fleet Variables** level instead, so you only have to set the `VEHICLE_ID` for new trucks).*

### Recommended `UPS_MAX_EXPECTED_AMPS` by hardware profile
Use conservative values that cover sustained load plus short transients. If `ovf` appears in logs during normal operation, increase this value one tier.

| Hardware profile | Typical sustained load | Recommended `UPS_MAX_EXPECTED_AMPS` |
| --- | --- | --- |
| Pi Zero 2 W + BerryGPS-IMU + standard LTE modem | 0.6A - 1.2A | `2.2` |
| Pi 3/4 class truck gateway + USB peripherals (moderate) | 1.2A - 2.0A | `3.2` |
| Pi 4 + camera / higher transient accessory load | 2.0A - 2.8A | `4.0` |

`UPS_MAX_EXPECTED_AMPS` is passed directly to INA219 calibration (`max_expected_amps`). Setting it too low causes false overflow behavior; setting it much higher than real load reduces current resolution.

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

## Phase 5: UPS Shutdown Validation Runbook
Use this runbook to validate low-battery debounce behavior and confirm shutdown event order from logs.

1. During a controlled battery drain test, tail live service logs:
   * `journalctl -fu <service>`
   * Confirm you see structured events in this order immediately before shutdown command execution:
     * `low_battery_detected`
     * `shutdown_requested`
     * `shutdown_command_result`
2. After the device reboots, inspect the previous boot logs to verify event ordering:
   * `journalctl -u <service> -b -1`
   * Validate that the same three events appear in-order and that timestamp/voltage/SOC/debounce fields are populated.
3. Capture power throttling state before and after the test:
   * `vcgencmd get_throttled`
   * Interpretation guidance:
     * `throttled=0x0`: no undervoltage or throttling flags were observed.
     * Non-zero bitmasks indicate power quality issues (for example, undervoltage now or historically). Investigate battery, wiring, and regulator margins if these bits are set after the test.

## Phase 6: Field WiFi Recovery (captive-portal hotspot)
The `wifi-provisioner` container handles the case where a deployed truck loses
its known WiFi (router replaced, PSK rotated, truck moved to a new yard) and
nobody can `balena ssh` because the device has no internet.

Behavior:
1. The container probes connectivity every `WIFI_PROVISIONER_CHECK_INTERVAL_SECONDS`
   (default 30s).
2. After `WIFI_PROVISIONER_GRACE_SECONDS` (default 180s ≈ 3 minutes) of
   continuous failure, it brings up a hotspot via NetworkManager:
   - SSID: `WIFI_PROVISIONER_SETUP_SSID` (default `smart-truck-setup-<vehicle_id>`)
   - Password: `WIFI_PROVISIONER_SETUP_PSK` (blank → open AP)
   - Device IP on the AP: `10.42.0.1`
3. Joining the SSID on a phone should pop the OS captive-portal sheet. Any URL
   redirects to the setup page at `http://10.42.0.1/`.
4. The page requires a PIN before any change is accepted. The PIN comes from
   `WIFI_PROVISIONER_SETUP_PIN`, or is derived from `VEHICLE_ID` and printed in
   the container's boot logs as:
   ```
   wifi-provisioner Setup hotspot SSID=... PIN=######
   ```
5. From the page you can add a new SSID/PSK, set a priority, or forget an
   existing saved network. Saved entries are normal `nmcli` profiles
   (`connection.autoconnect=yes`), so the device keeps a full list — not just
   the most recent network — and NetworkManager picks the strongest reachable
   one on boot.
6. Every `WIFI_PROVISIONER_HOTSPOT_RETRY_INTERVAL_SECONDS` (default 120s) the
   container drops the hotspot for `WIFI_PROVISIONER_HOTSPOT_RETRY_PROBE_SECONDS`
   (default 45s) so NetworkManager can attempt the saved profiles. If
   connectivity returns, the hotspot stays down; otherwise it re-raises and
   waits for human action.

Operator notes:
- Set `WIFI_PROVISIONER_SETUP_PIN` at the fleet level so techs in the field
  use a memorable shared secret instead of the hash-derived per-device PIN.
- Set `WIFI_PROVISIONER_SETUP_PSK` to a fleet-wide WPA2 password if your
  trucks ever park somewhere a passer-by could see the open SSID.
- To pre-load several known networks before deployment, `balena ssh` into the
  device and run `nmcli c add type wifi con-name "<ssid>" ifname wlan0 ssid
  "<ssid>" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "<psk>"
  connection.autoconnect yes connection.autoconnect-priority <n>` for each
  network. The provisioner will see them on its next supervisor cycle.
