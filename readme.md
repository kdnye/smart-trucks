

# Smart Truck Telematics Fleet Management

This repository contains the containerized telematics stack for independent GPS, temperature, and geofence monitoring, optimized for deployment via **Balena** on Raspberry Pi (Zero 2 W / 3B). 

The system provides a forward-thinking solution for cold-chain logistics, ensuring data integrity across intermittent Wi-Fi connections and power-conscious operation.

## Architecture Overview
The stack is structured into decoupled services to allow for modular OTA (Over-the-Air) updates and resource optimization.

**Active services (base-model stack):**

* **`gps-multiplexer`**: Reads raw NMEA from the hardware UART and broadcasts it over TCP (port 2947) so other containers never touch the serial port directly.
* **`telematics-edge`**: Reads GPS via TCP + BerryIMU metrics via I2C, manages the SQLite WAL store, and builds heartbeat / edge-health payloads.
* **`ble-sensor`**: Scans for Govee BLE advertisements. Extracts temperature, humidity, and battery health. Runs at full scan cadence (battery-saver disabled — there is no active `power_readings` writer in the base-model stack).
* **`sync-service`**: Drains the SQLite store-and-forward queues (`gps_points`, `heartbeats`, `edge_health`, `ble_scans`) and POSTs them to the cloud ingest endpoint.

> **Parked (not active in the current base-model compose):**
> **`power-monitor`** — Dedicated UPS HAT (B) / power-board telemetry service that reads battery metrics via I2C and persists snapshots to `power_readings`. It is removed from the active stack; with it gone, `UPS_*`/`POWER_*` variables are not wired and BLE battery-saver is disabled. `telematics-edge` still creates an empty `power_readings` table for forward compatibility. Restore details: see `EDGE_DASHBOARD_CONTRACT.md` § "Parked / to reintegrate later".

## Project Structure
```text
telematics-fleet/
├── docker-compose.yml
├── ble-sensor/
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── telematics-edge/
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
└── power-monitor/
    ├── Dockerfile
    ├── main.py
    └── requirements.txt
```

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

> ⚠️ Set `RESIN_HOST_CONFIG_dtparam` exactly as `i2c_arm=on,spi=on` **without quotation marks** to avoid I2C bus failures in the `power-monitor` container.

## Fleet Variables
Use **Environment Variables** in Balena to manage unique truck settings without code changes:

> Note: UPS / INA219 variables (`UPS_*`, `POWER_*`) are consumed by the **parked** `power-monitor` service and are **not wired in the current base-model stack**. They are retained below for when power-board monitoring is reintegrated (see `EDGE_DASHBOARD_CONTRACT.md` § "Parked / to reintegrate later"). `telematics-edge` does not read INA219 directly.

| Variable | Description | Example |
| :--- | :--- | :--- |
| `VEHICLE_ID` | Unique identifier for the truck | `TRK-905` |
| `WEBHOOK_URL` | Endpoint for Motive Dashboard sync | `https://api.yourdomain.com/telematics` |
| `EDGE_INGEST_KEY` | Preferred auth key sent as the `X-API-Key` header by `sync-service`. Falls back to `MOTIVE_API_KEY` if unset (transitional). | `eik_...` |
| `MOTIVE_API_KEY` | Legacy/transitional auth key; used as the `X-API-Key` fallback until the fleet is cut over to `EDGE_INGEST_KEY`. | `mk_...` |
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

1. **UART contention or unstable GPS serial stream**
   * Signature: `Serial read failed: device reports readiness to read but returned no data` in `telematics-edge`.
   * First checks:
     * Ensure only one process owns `/dev/serial0`.
     * Ensure `telematics-edge` has direct `/dev/serial0` mapping (no user-space serial fan-out).
     * In BalenaCloud Fleet Configuration, add a custom udev rule to hide Pi UART from ModemManager:
       ```json
       {
         "99-ignore-serial": "SUBSYSTEM==\"tty\", KERNEL==\"serial0|ttyAMA0|ttyS0\", ENV{ID_MM_DEVICE_IGNORE}=\"1\""
       }
       ```
     * Verify GPS wiring and power stability.
     * Confirm `GPS_SERIAL_DEVICE` and `GPS_SERIAL_CANDIDATES` are set consistently.
   * `gps-multiplexer/start.sh` behavior:
     * Missing serial device (for example `/dev/serial0`) logs a warning and continues.
     * Missing Python app entrypoint (`/usr/src/app/main.py`) exits immediately.
   * Serial candidate discovery is implemented in `gps-multiplexer/main.py` via `GPS_SERIAL_CANDIDATES`.

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
   * Recovery order:
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
