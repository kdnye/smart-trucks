

# Smart Truck Telematics Fleet Management

This repository contains the containerized telematics stack for independent GPS, temperature, and geofence monitoring, optimized for deployment via **Balena** on Raspberry Pi (Zero 2 W / 3B). 

The system provides a forward-thinking solution for cold-chain logistics, ensuring data integrity across intermittent Wi-Fi connections and power-conscious operation.

## Architecture Overview
The stack runs as **two containers** on the Pi, split by failure domain — a data
plane and a network/control plane — to keep RAM and Balena supervisor churn low
on the 512 MB Pi Zero 2 W:

**Active services (base-model stack):**

* **`edge`** (data plane, host networking, privileged): one container that runs
  three co-processes — supervised by `edge/start.sh`, each restarted on exit:
  * *telematics-edge* — reads GPS **directly from the serial UART**
    (`/dev/serial0`; the old `gps-multiplexer` TCP broker is gone) + BerryIMU
    metrics via I2C, manages the SQLite WAL store, and builds heartbeat /
    edge-health payloads.
  * *ble-sensor* — scans for Govee BLE advertisements (temperature, humidity,
    battery) over the host BlueZ/D-Bus and enqueues them to the shared SQLite
    queue. Battery-saver disabled (no active `power_readings` writer in the base
    stack).
  * *sync-service* — drains the SQLite store-and-forward queues (`gps_points`,
    `heartbeats`, `edge_health`, `ble_scans`) and POSTs them to the cloud ingest
    endpoint.
* **`wifi-provisioner`** (control plane, host networking, privileged): the **sole
  owner of `wlan0`/NetworkManager** — saved-network reconnect + roaming and the
  setup-AP captive portal (see `docs/WIFI_PROVISIONING.md`) — **and** the
  cross-service watchdog that restarts the `edge` service via the Balena
  Supervisor API when its heartbeats go stale. Serves `/healthz` (this absorbed
  the former standalone `health` container).

> The earlier per-service containers (`gps-multiplexer`, `ble-sensor`,
> `sync-service`, `health`, and the standalone `telematics-edge`) were
> consolidated into the two above. Each service's code is unchanged; only the
> packaging (one image, co-processes) and the GPS path (direct serial) changed.

> **Parked (not active in the current base-model compose):**
> **`power-monitor`** — Dedicated UPS HAT (B) / power-board telemetry service that reads battery metrics via I2C and persists snapshots to `power_readings`. It is removed from the active stack; with it gone, `UPS_*`/`POWER_*` variables are not wired and BLE battery-saver is disabled. `telematics-edge` still creates an empty `power_readings` table for forward compatibility. Restore details: see `EDGE_DASHBOARD_CONTRACT.md` § "Parked / to reintegrate later".

## Project Structure
```text
telematics-fleet/
├── docker-compose.yml         # 2 services: edge + wifi-provisioner
├── edge/                      # unified data-plane image
│   ├── Dockerfile             # installs all three services' deps
│   ├── requirements.txt
│   └── start.sh               # co-process supervisor (restarts each on exit)
├── telematics-edge/           # GPS (direct serial) + IMU + heartbeats  (co-process)
│   ├── main.py
│   └── requirements.txt
├── ble-sensor/                # Govee BLE scanning → ble_scans            (co-process)
│   ├── main.py
│   └── requirements.txt
├── sync-service/              # drains SQLite queues → cloud              (co-process)
│   ├── main.py
│   └── requirements.txt
├── wifi-provisioner/          # wlan0/NetworkManager + captive portal + edge watchdog
│   ├── Dockerfile
│   ├── main.py
│   └── edge_watchdog.py
└── shared/                    # shared libs: sqlite_util, env, sentry_flag, balena_supervisor
```
> `power-monitor/` remains in the repo but is **parked** (not in the active
> compose). The per-service `Dockerfile`s under `telematics-edge/`, `ble-sensor/`,
> and `sync-service/` are superseded by `edge/Dockerfile` (kept for reference).

## Hardware Requirements
* **SBC**: Raspberry Pi Zero 2 W (Production) or Pi 3B (Testing).
* **GPS**: BerryGPS-IMU V4 (OzzMaker).
* **Sensors**: Govee Bluetooth Hygrometer/Thermometer.
* **Power**: Pi Zero UPS HAT (B) with Solar Input.
* **Antenna**: Bingfu Waterproof Active GPS Antenna.

## Quick Start: Deployment

For a full, field-tested setup workflow for brand-new devices (Balena host config, identity variables, GPS cold start, and dashboard verification), see the **[Device Provisioning & Dashboard Sync Guide](docs/device-provisioning-guide.md)**.

For UPS shutdown tuning, use the **[Hold-Up Characterization Test Procedure](docs/hold-up-characterization-test-procedure.md)** to capture per-hardware-profile recommended low-battery thresholds with 20–60s hold-up headroom.

### 1. Prerequisites
* Install the [Balena CLI](https://github.com/balena-io/balena-cli).
* Create a Fleet in your BalenaCloud dashboard.

### 2. Push to Fleet
```bash
# Clone the repository
git clone <repo-url>
cd telematics-fleet

# Push to your balena fleet
balena push <fleet-name>
```

### 3. Device Configuration
Set the following variables in the **Balena Dashboard > Device Configuration** to enable hardware interfaces:
* `RESIN_HOST_CONFIG_dtparam`: `i2c_arm=on,spi=on` (**no quotes**)
* `RESIN_HOST_CONFIG_enable_uart`: `1`
* `RESIN_HOST_CONFIG_dtoverlay`: `disable-bt` *(required on Pi Zero 2 W and Pi 3+)*

> ⚠️ Set `RESIN_HOST_CONFIG_dtparam` exactly as `i2c_arm=on,spi=on` **without quotation marks** to avoid I2C bus failures in the `power-monitor` container.

> ⚠️ **Pi Zero 2 W / Pi 3+ UART pitfall:** by default the full-featured PL011 UART (`/dev/ttyAMA0`) is owned by the Bluetooth firmware and `/dev/serial0` is symlinked to the **mini-UART** (`/dev/ttyS0`), whose clock varies with CPU frequency and is unstable for NMEA at 9600 baud. Setting `RESIN_HOST_CONFIG_dtoverlay=disable-bt` reassigns `serial0` to `ttyAMA0`. Without it, `telematics-edge` either finds no usable device (no `enable_uart=1`) or reads corrupt sentences (`enable_uart=1` only). Both `enable_uart` and `dtoverlay=disable-bt` are required for stable GPS on these boards.

## Fleet Variables
Use **Environment Variables** in Balena to manage unique truck settings without code changes:

> Note: UPS / INA219 variables (`UPS_*`, `POWER_*`) are consumed by the **parked** `power-monitor` service and are **not wired in the current base-model stack**. They are retained below for when power-board monitoring is reintegrated (see `EDGE_DASHBOARD_CONTRACT.md` § "Parked / to reintegrate later"). `telematics-edge` does not read INA219 directly.

| Variable | Description | Example |
| :--- | :--- | :--- |
| `VEHICLE_ID` | Unique identifier for the truck | `TRK-905` |
| `WEBHOOK_URL` | Endpoint for Motive Dashboard sync | `https://api.yourdomain.com/telematics` |
| `EDGE_INGEST_KEY` | Auth key sent as the `X-API-Key` header by `sync-service`; must match the cloud ingest function's `EDGE_INGEST_KEY`. | `eik_...` |
| `POLL_INTERVAL` | Seconds between cloud syncs | `300` |
| `TEMP_THRESHOLD` | Critical temp alert trigger (Celsius) | `4.0` |
| `UPS_I2C_ADDRESS` | I2C address for UPS INA219 monitor. | `0x43` |
| `UPS_I2C_ADDRESS_CANDIDATES` | Optional comma-separated fallback INA219 addresses (hex) to try if the primary address is unavailable. | `0x43,0x40,0x41,0x44,0x45` |
| `POWER_SAMPLE_INTERVAL_SECONDS` | Power monitor sample/upload cadence in seconds. | `2` |
| `POWER_UPLOAD_BATCH_SIZE` | Number of power samples uploaded per batch. | `50` |
| `UPS_SHUNT_OHMS` | INA219 shunt resistor value in ohms for UPS current/power calibration. | `0.01` |
| `UPS_BATTERY_CAPACITY_MAH` | Battery-pack capacity used for runtime estimation in `power-monitor` payloads. | `4400` |
| `UPS_MIN_DISCHARGE_CURRENT_MA_FOR_RUNTIME_ESTIMATE` | Ignore tiny discharge current noise below this threshold when estimating remaining runtime. | `20` |
| `NETWORK_WATCHDOG_ENABLED` | Enables periodic Wi-Fi recovery logic (`nmcli` rescan/radio reset) in `telematics-edge`. | `true` |
| `NETWORK_WATCHDOG_CHECK_INTERVAL_SECONDS` | Seconds between connectivity checks before recovery is attempted. | `60` |
| `NETWORK_WATCHDOG_MAX_FAILURES` | Consecutive failed checks required before Wi-Fi recovery commands run. | `3` |
| `NETWORK_WATCHDOG_RECOVERY_PAUSE_SECONDS` | Cooldown after recovery commands before checks resume. | `30` |
| `NETWORK_WATCHDOG_CONNECTION_NAME` | Optional `nmcli` connection profile name to explicitly bring up after recovery. | `""` |
| `IMU_I2C_BUS` | I2C bus used for IMU probing and telematics IMU reads. | `1` |
| `IMU_EXPECTED_ADDRESSES` | Comma-separated IMU address candidates (hex) expected on `IMU_I2C_BUS`. | `0x6A` |
| `IMU_REQUIRED` | Fail fast when no IMU is detected at startup (`true`/`false`). | `true` |
| `WIFI_PROVISIONER_SETUP_SSID` | SSID of the captive-portal hotspot raised by `wifi-provisioner` when no saved network is reachable. Defaults to `smart-truck-setup-<vehicle_id>`. | `smart-truck-setup-trk-905` |
| `WIFI_PROVISIONER_SETUP_PSK` | Optional WPA2 password for the setup hotspot. Leave blank for an open AP (recommended only in physically controlled yards). | `""` |
| `WIFI_PROVISIONER_SETUP_PIN` | PIN required by the captive portal before it will save or delete a saved network. If unset, derived from `VEHICLE_ID` and logged on boot. | `137204` |
| `WIFI_PROVISIONER_GRACE_SECONDS` | How long the truck stays offline before raising the setup hotspot. | `180` |
| `WIFI_PROVISIONER_CHECK_INTERVAL_SECONDS` | Connectivity probe cadence (seconds). | `30` |
| `WIFI_PROVISIONER_HOTSPOT_RETRY_INTERVAL_SECONDS` | While the hotspot is up, how often to drop it and let NetworkManager try saved profiles. | `120` |
| `WIFI_PROVISIONER_HOTSPOT_RETRY_PROBE_SECONDS` | After a retry drop, how long to wait for a client connection before re-raising the hotspot. | `45` |

> ⚠️ `UPS_SHUNT_OHMS` must match your UPS HAT hardware. A `0.1` vs `0.01` mismatch causes approximately **10x error** in INA219 current/power telemetry.

> ⚠️ `VEHICLE_ID` is required for truck identity. Do **not** rely on `MOTIVE_TRUCK_NUMBER`; if `VEHICLE_ID` is missing, payloads default to `UNKNOWN_TRUCK`.

> ⚠️ If you run two 18650 cells in parallel, set `UPS_BATTERY_CAPACITY_MAH` to the combined pack capacity (for example `4400` for two 2200 mAh cells). Before paralleling cells, verify they are within 0.1V of each other.


### BLE Sensor Service Configuration
The `ble-sensor` container can run in a privacy-preserving mode by default:

| Variable | Description | Default |
| :--- | :--- | :--- |
| `SCAN_DURATION_SECONDS` | BLE scan window length per cycle. | `15` |
| `POLL_INTERVAL` | Seconds between each scan/upload cycle. | `60` |
| `ANONYMIZE_MAC` | When `true`, hashes device MAC addresses before upload. Default `false` so `device_id` carries the raw canonical MAC that matches the `ble_tag_id` provisioned in the Lifecycle inventory app. | `false` |
| `MAC_HASH_SALT` | Secret salt used to hash MAC addresses. Set per deployment. | `""` |
| `INCLUDE_DEVICE_NAME` | Includes BLE local names in payloads when enabled. | `false` |
| `MAX_DEVICES_PER_SCAN` | Caps payload size in busy RF environments. | `500` |
| `HTTP_TIMEOUT_SECONDS` | Upload timeout in seconds. | `10` |
| `BLE_LOCAL_DB_PATH` | Local SQLite WAL path used to queue BLE scan events for offline backfill. | `/data/ble-sensor.db` |
| `UPLOAD_BATCH_SIZE` | Max queued BLE scan events uploaded per cycle after connectivity returns. | `25` |

`ble-sensor` now stores each scan result (including `sensor_count=0` windows) to local SQLite before upload attempts. This preserves both inventory presence and absence history so dashboards can backfill accurately after a connectivity outage.

> ⚠️ Legal/Privacy note: only collect BLE broadcasts where you have explicit permission and a lawful basis to process device identifiers.

## Recovering a truck whose WiFi changed
If a truck loses its known WiFi (new router, rotated PSK, swapped yard), the
`wifi-provisioner` service raises a captive-portal hotspot after
`WIFI_PROVISIONER_GRACE_SECONDS` (default 3 minutes) of being offline:

1. On a phone, join the SSID `smart-truck-setup-<vehicle_id>` (or whatever you
   set `WIFI_PROVISIONER_SETUP_SSID` to). With `WIFI_PROVISIONER_SETUP_PSK`
   blank the AP is open; otherwise enter the PSK you configured.
2. The phone's captive-portal sheet should pop open automatically. If it does
   not, browse to `http://10.42.0.1/`.
3. Enter the new SSID + password and the setup PIN. The PIN is logged on boot
   (look for `Setup hotspot SSID=... PIN=...` in Balena device logs) or set
   explicitly via `WIFI_PROVISIONER_SETUP_PIN`.
4. After saving, the hotspot drops in ~30–60 seconds and the truck rejoins
   using the new (or previously saved) network. Saved profiles persist across
   reboots; NetworkManager picks the highest-priority one that is in range.

The captive portal also lets you forget saved networks. All known networks are
stored as NetworkManager connection profiles, not in an app-managed list — you
can also add/remove them from Balena's `balena ssh` shell using `nmcli c`.

## Power Optimization (Battery / Solar)
For solar/battery deployments, the services throttle their highest-cost work
(IMU polling, BLE scanning, INA219 sampling) when the truck is parked and on
battery, and resume full cadence when driving or charging. See
[`docs/power-optimization.md`](docs/power-optimization.md) for the tunable
environment variables (`IMU_PARKED_SAMPLE_INTERVAL_SECONDS`, `BLE_*`,
`POWER_SAMPLE_INTERVAL_IDLE_SECONDS`, ...) and host-level recommendations
(disable HDMI/LEDs, Wi-Fi power-save).

## Technical Integration Points
* **Power Management**: Monitors INA219 chip on the UPS HAT via I2C to trigger low-power modes.
* **Data Optimization**: Uses Balena's binary delta updates to minimize data consumption on limited 2GB monthly cellular plans.
* **Storage**: Local SQLite persistence for offline buffering during transit.

---
**Status**: Development / Testing Phase (Pi 3B)  
**Maintainer**: Dave Alexander


### Check device access across all containers
When you hit errors like `No such file or directory: '/dev/i2c-1'`, verify both compose mappings and runtime visibility for every service:

```bash
./scripts/check-container-hw-access.sh
```

If containers are not running yet, audit configuration only:

```bash
./scripts/check-container-hw-access.sh --config-only
```

Expected minimum mappings:
* `telematics-edge`: `/dev/i2c-1` and `/dev/serial0`
* `power-monitor`: `/dev/i2c-1`

If runtime checks return `no`, redeploy with the updated `docker-compose.yml` and confirm host-level I2C/UART are enabled in Balena Device Configuration.

## Balena Runtime Troubleshooting (Observed April 14, 2026)

The following patterns are expected during supervised updates and should not be treated as hard failures on their own:

* Transient image fetch aborts like `Failed to download image ... The operation was aborted` immediately followed by successful retries/downloads.
* Repeated `Taking update locks` / `Releasing update locks` while services are replaced.
* Service `Killing ...` and `Installing ...` lines during delta-based image rollouts.

### High-signal issues to act on

1. **`telematics-edge` reports no usable GPS serial device** (GPS is read directly from the UART now — there is no `gps-multiplexer`)
   * Signature: `Primary GPS serial path unavailable. device=/dev/serial0 errno=2 ...` in the `edge` container, and/or the boot hardware-inventory log showing `"nmea_detected": false` for `/dev/serial0`.
   * Most common cause on **Pi Zero 2 W / Pi 3+**: missing host UART config. Set in Balena Device or Fleet Configuration:
     * `RESIN_HOST_CONFIG_enable_uart` = `1`
     * `RESIN_HOST_CONFIG_dtoverlay` = `disable-bt`
     * Reboot. After the reboot, `/dev/serial0` should point at `/dev/ttyAMA0` (the stable PL011 UART) and `telematics-edge` will log `Successfully connected to /dev/serial0`.
   * If the GPS is **USB** instead of GPIO: confirm it shows up on the host as `/dev/ttyACM0` or `/dev/ttyUSB0`. For other paths, set `GPS_SERIAL_CANDIDATES` in Balena to a comma-separated list — e.g. `GPS_SERIAL_CANDIDATES=/dev/ttyACM0,/dev/ttyUSB0,/dev/serial0`.
   * Other useful checks:
     * Ensure only one process owns `/dev/serial0` — `telematics-edge` is the only reader of the UART in the `edge` container.
     * Verify GPS wiring and power stability — a blinking fix LED + voltage on the TX/RX pins proves the receiver is talking, but if the kernel never created the device `telematics-edge` can't read it.
     * In BalenaCloud Fleet Configuration, hide Pi UART from ModemManager so it doesn't grab the port at boot:
       ```json
       {
         "99-ignore-serial": "SUBSYSTEM==\"tty\", KERNEL==\"serial0|ttyAMA0|ttyS0\", ENV{ID_MM_DEVICE_IGNORE}=\"1\""
       }
       ```
   * Serial candidate selection lives in `telematics-edge/main.py::gps_reader_worker` (it picks the first existing candidate), and the boot probe in `shared/hardware_probe.py::probe_nmea_candidates` logs which paths exist and which produced NMEA.

2. **UART contention or corrupt NMEA sentences**
   * Signature: `Serial read failed: device reports readiness to read but returned no data`, or NMEA checksum errors, from `telematics-edge`.
   * First check: are you on the mini-UART (`/dev/ttyS0`)? Its clock varies with CPU frequency — on Pi Zero 2 W / Pi 3+ you must set `RESIN_HOST_CONFIG_dtoverlay=disable-bt` so `serial0 → ttyAMA0` (the stable PL011) instead.
   * `edge/start.sh` runs `stty -F "$GPS_SERIAL_DEVICE" 9600 raw -echo` before launching the co-processes; a missing serial device logs a warning and continues (non-fatal).

2. **Missing optional pyserial dependency in power-monitor probe path**
   * Signature: `pyserial unavailable: No module named 'serial'` inside `power-monitor` hardware inventory output.
   * Interpretation: This only affects NMEA probe diagnostics in `power-monitor`; UPS telemetry can still be healthy.

3. **Power state stuck at `overflow_fault`**
   * Signature: `Stored power reading: ok state=overflow_fault` on every sample.
   * First checks:
     * Validate `UPS_SHUNT_OHMS` matches your physical INA219 shunt value.
     * Reconfirm INA219 address and wiring (default candidate list: `0x43,0x40,0x41,0x44,0x45`).

4. **BLE scan start contention (`org.bluez.Error.InProgress`)**
   * Signatures:
     * `org.bluez.Error.InProgress`
     * `BlueZ scan-start contention detected`
   * Automated recovery (default on), in escalating order once contention is persistent:
     1. **Adapter power-cycle** — `ble-sensor` toggles the adapter over BlueZ
        D-Bus (`Adapter1.Powered` off→on, the equivalent of `hciconfig hci0
        reset`), at most once per `SCAN_CONTENTION_ADAPTER_RESET_INTERVAL_SECONDS`
        (default 300s). Re-issuing `StartDiscovery` alone can never clear the
        wedge — it is the call that keeps failing. Note: BlueZ refuses `Powered
        off` with `Busy` while a discovery is active, and some images ship no
        `rfkill`/`hciconfig`, so this step can be a no-op — hence step 2. Disable
        with `SCAN_CONTENTION_ADAPTER_RESET_ENABLED=false`; target a non-default
        radio with `BLE_ADAPTER`.
     2. **Supervised self-restart** — if contention stays unrecovered for
        `SCAN_CONTENTION_RESTART_AFTER_SECONDS` (default 600s), `ble-sensor` exits
        so `edge/start.sh` respawns it with a fresh BlueZ D-Bus connection, which
        lets BlueZ reap the stale discovery session (the automated equivalent of
        the manual "restart the ble-sensor service" below). `ble-sensor` is its
        own supervised process, so this does not disturb GPS/heartbeats.
   * Manual recovery order (if the automated steps can't clear it):
     1. First, restart only the `ble-sensor` service.
     2. If BLE scan start remains stuck, reboot the device from the Balena Dashboard to power-cycle `hci0`.
   * Prevention:
     * Avoid force-killing the scanner container during an active scan when possible.
     * Prefer graceful stop/redeploy paths so BlueZ can release scan state cleanly.

### Alerting policy reminder

Operational alert transport should remain account-managed and routed through **Postmark**.

## IMU Calibration Container (On-Demand)

An optional `imu-calibration` service can be launched independently from Balena when you need labeled jostle-event datasets for model tuning.

### What it provides
- Browser UI at `http://<device-ip>:8085` with:
  - Start/Stop recording controls
  - Live magnitude graph from the IMU stream
  - 12 preconfigured event buttons (`door_open`, `door_close`, `pothole`, `speed_bump`, `railroad_track`, `hard_brake`, `hard_accel`, `sharp_left_turn`, `sharp_right_turn`, `rough_road`, `curb_impact`, `cargo_shift`)
  - Custom event + notes entry
- Local SQLite WAL data capture to `/data/imu-calibration.db` (`calibration_sessions`, `imu_samples`, `calibration_marks`).
- Session/event labels can later be converted into a truth-table style profile JSON for telematics tuning.

### Operational trigger (dormant by default)
- `imu-calibration` starts in dormant mode unless `ENABLE_CALIBRATION=true`.
- In Balena, set `ENABLE_CALIBRATION=true` as a **service-scoped device variable** targeting only the `imu-calibration` service when you want to run a calibration session.
- After calibration completes, reset or remove that service variable so the container returns to dormant behavior.

### Suggested field workflow
1. Pause heavier containers in Balena (optional for resource headroom).
2. Start only `imu-calibration`.
3. Open the UI and record a session while marking events.
4. Stop recording and stop `imu-calibration`.
5. Restart normal containers.
6. Run truth-table sync on the telematics container:

```bash
python telematics-edge/scripts/sync_imu_calibration_truth_table.py \
  --db-path /data/imu-calibration.db \
  --output /data/imu-truth-table.json
```

The generated `/data/imu-truth-table.json` can be consumed by downstream threshold/model experiments without changing runtime hot-path sensor loops.

## BLE Distance + Obstruction Calibration Container (On-Demand)

An optional `ble-calibration` service is available for per-device BLE environment calibration (distance + obstruction effects) without modifying production scan loops.

### What it provides
- Browser UI for interactive mapping:
  - configure space size in meters (grid uses 1m x 1m cells),
  - set Pi location,
  - mark obstruction cells (walls, pallets, etc.),
  - assign currently detected beacons to clicked grid cells and capture RSSI samples.
- BLE scanning with periodic refresh to keep a current beacon list.
- SQLite WAL capture to `/data/ble-calibration.db`:
  - `ble_calibration_sessions`
  - `ble_grid_state`
  - `ble_captures`
- Per-beacon local path-loss fit for clear and obstructed paths (`RSSI = intercept + slope*log10(distance_m)`) with an obstruction offset estimate.

### Operational trigger (dormant by default)
- `ble-calibration` starts in dormant mode unless `ENABLE_CALIBRATION=true`.
- In Balena, set `ENABLE_CALIBRATION=true` as a **service-scoped device variable** targeting only the `ble-calibration` service when you need a BLE calibration run.
- After the run completes, reset or remove that service variable to restore dormant-by-default operation.

### Suggested workflow
1. Pause heavy containers in Balena (optional).
2. Start `ble-calibration` on the target Pi.
3. Open `http://<device-ip>:8080` (host networking).
4. Enter grid dimensions, set Pi location, and mark obstruction cells.
5. For each beacon, collect 4-5 captures across multiple positions.
6. Repeat after moving Pi mounting location (cab/box placement experiments).
7. Stop session and container when done.

This creates a reusable per-Pi calibration dataset that can be used later to improve beacon distance inference under truck-specific environmental conditions.
