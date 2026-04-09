

# Smart Truck Telematics Fleet Management

This repository contains the containerized telematics stack for independent GPS, temperature, and geofence monitoring, optimized for deployment via **Balena** on Raspberry Pi (Zero 2 W / 3B). 

The system provides a forward-thinking solution for cold-chain logistics, ensuring data integrity across intermittent Wi-Fi connections and power-conscious operation.

## Architecture Overview
The stack is structured into three decoupled services to allow for modular OTA (Over-the-Air) updates and resource optimization:

* **`ble-sensor`**: Scans for Govee BLE advertisements. Extracts temperature, humidity, and battery health.
* **`gps-telematics`**: Interchanges with the BerryGPS-IMU V4 via UART. Processes NMEA sentences and evaluates geofencing logic.
* **`sync-service`**: Manages the data queue. Synchronizes logs with the cloud dashboard via webhooks when connectivity is available.

## Project Structure
```text
telematics-fleet/
├── docker-compose.yml
├── ble-sensor/
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── gps-telematics/
│   ├── Dockerfile
│   ├── process_data.py
│   └── requirements.txt
└── sync-service/
    ├── Dockerfile
    ├── sync.py
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
* `RESIN_HOST_CONFIG_dtparam`: `"i2c_arm=on","spi=on"`
* `RESIN_HOST_CONFIG_enable_uart`: `1`

## Fleet Variables
Use **Environment Variables** in Balena to manage unique truck settings without code changes:

| Variable | Description | Example |
| :--- | :--- | :--- |
| `VEHICLE_ID` | Unique identifier for the truck | `TRK-905` |
| `WEBHOOK_URL` | Endpoint for Motive Dashboard sync | `https://api.yourdomain.com/telematics` |
| `POLL_INTERVAL` | Seconds between cloud syncs | `300` |
| `TEMP_THRESHOLD` | Critical temp alert trigger (Celsius) | `4.0` |

## Technical Integration Points
* **Power Management**: Monitors INA219 chip on the UPS HAT via I2C to trigger low-power modes.
* **Data Optimization**: Uses Balena's binary delta updates to minimize data consumption on limited 2GB monthly cellular plans.
* **Storage**: Local SQLite persistence for offline buffering during transit.

---
**Status**: Development / Testing Phase (Pi 3B)  
**Maintainer**: Dave Alexander
