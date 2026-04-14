

# Smart Truck Telematics Fleet Management

This repository contains the containerized telematics stack for independent GPS, temperature, and geofence monitoring, optimized for deployment via **Balena** on Raspberry Pi (Zero 2 W / 3B). 

The system provides a forward-thinking solution for cold-chain logistics, ensuring data integrity across intermittent Wi-Fi connections and power-conscious operation.

## Architecture Overview
The stack is structured into decoupled services to allow for modular OTA (Over-the-Air) updates and resource optimization:

* **`ble-sensor`**: Scans for Govee BLE advertisements. Extracts temperature, humidity, and battery health.
* **`telematics-edge`**: Reads BerryGPS UART data and BerryIMU metrics via I2C, then syncs unified payloads to the cloud.
* **`power-monitor`**: Dedicated UPS HAT (B) telemetry service that reads INA219 battery metrics via I2C, persists snapshots locally, and optionally syncs power events upstream.

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
* `BALENA_HOST_CONFIG_dtparam`: `"i2c_arm=on","spi=on"`
* `BALENA_HOST_CONFIG_enable_uart`: `1`

## Fleet Variables
Use **Environment Variables** in Balena to manage unique truck settings without code changes:

| Variable | Description | Example |
| :--- | :--- | :--- |
| `VEHICLE_ID` | Unique identifier for the truck | `TRK-905` |
| `WEBHOOK_URL` | Endpoint for Motive Dashboard sync | `https://api.yourdomain.com/telematics` |
| `POLL_INTERVAL` | Seconds between cloud syncs | `300` |
| `TEMP_THRESHOLD` | Critical temp alert trigger (Celsius) | `4.0` |
| `UPS_I2C_ADDRESS` | I2C address for UPS INA219 monitor. | `0x43` |
| `UPS_I2C_ADDRESS_CANDIDATES` | Optional comma-separated fallback INA219 addresses (hex) to try if the primary address is unavailable. | `0x43,0x40,0x41,0x44,0x45` |
| `POWER_SAMPLE_INTERVAL_SECONDS` | Power monitor sample/upload cadence in seconds. | `2` |
| `POWER_UPLOAD_BATCH_SIZE` | Number of power samples uploaded per batch. | `50` |
| `UPS_SHUNT_OHMS` | INA219 shunt resistor value in ohms for UPS current/power calibration. | `0.01` |
| `IMU_I2C_BUS` | I2C bus used for IMU probing and telematics IMU reads. | `1` |
| `IMU_EXPECTED_ADDRESSES` | Comma-separated IMU address candidates (hex) expected on `IMU_I2C_BUS`. | `0x6A` |
| `IMU_REQUIRED` | Fail fast when no IMU is detected at startup (`true`/`false`). | `true` |

> ⚠️ `UPS_SHUNT_OHMS` must match your UPS HAT hardware. A `0.1` vs `0.01` mismatch causes approximately **10x error** in INA219 current/power telemetry.


### BLE Sensor Service Configuration
The `ble-sensor` container can run in a privacy-preserving mode by default:

| Variable | Description | Default |
| :--- | :--- | :--- |
| `SCAN_DURATION_SECONDS` | BLE scan window length per cycle. | `15` |
| `POLL_INTERVAL` | Seconds between each scan/upload cycle. | `60` |
| `ANONYMIZE_MAC` | Hashes device MAC addresses before upload. | `true` |
| `MAC_HASH_SALT` | Secret salt used to hash MAC addresses. Set per deployment. | `""` |
| `INCLUDE_DEVICE_NAME` | Includes BLE local names in payloads when enabled. | `false` |
| `MAX_DEVICES_PER_SCAN` | Caps payload size in busy RF environments. | `500` |
| `HTTP_TIMEOUT_SECONDS` | Upload timeout in seconds. | `10` |

> ⚠️ Legal/Privacy note: only collect BLE broadcasts where you have explicit permission and a lawful basis to process device identifiers.

## Technical Integration Points
* **Power Management**: Monitors INA219 chip on the UPS HAT via I2C to trigger low-power modes.
* **Data Optimization**: Uses Balena's binary delta updates to minimize data consumption on limited 2GB monthly cellular plans.
* **Storage**: Local SQLite persistence for offline buffering during transit.

---
**Status**: Development / Testing Phase (Pi 3B)  
**Maintainer**: Dave Alexander

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
     * Verify GPS wiring and power stability.
     * Confirm `GPS_SERIAL_DEVICE` and `GPS_SERIAL_CANDIDATES` are set consistently.

2. **Missing optional pyserial dependency in power-monitor probe path**
   * Signature: `pyserial unavailable: No module named 'serial'` inside `power-monitor` hardware inventory output.
   * Interpretation: This only affects NMEA probe diagnostics in `power-monitor`; UPS telemetry can still be healthy.

3. **Power state stuck at `overflow_fault`**
   * Signature: `Stored power reading: ok state=overflow_fault` on every sample.
   * First checks:
     * Validate `UPS_SHUNT_OHMS` matches your physical INA219 shunt value.
     * Reconfirm INA219 address and wiring (default candidate list: `0x43,0x40,0x41,0x44,0x45`).

### Alerting policy reminder

Operational alert transport should remain account-managed and routed through **Postmark**.
