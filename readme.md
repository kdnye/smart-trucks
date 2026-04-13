

# Smart Truck Telematics Fleet Management

This repository contains the containerized telematics stack for independent GPS, temperature, and geofence monitoring, optimized for deployment via **Balena** on Raspberry Pi (Zero 2 W / 3B). 

The system provides a forward-thinking solution for cold-chain logistics, ensuring data integrity across intermittent Wi-Fi connections and power-conscious operation.

## Architecture Overview
The stack is structured into decoupled services to allow for modular OTA (Over-the-Air) updates and resource optimization:

* **`ble-sensor`**: Scans for Govee BLE advertisements. Extracts temperature, humidity, and battery health.
* **`telematics-edge`**: Reads BerryGPS UART data, BerryIMU metrics via I2C, and Solar UPS HAT power telemetry, then syncs unified payloads to the cloud.

## Project Structure
```text
telematics-fleet/
├── docker-compose.yml
├── ble-sensor/
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
└── telematics-edge/
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
