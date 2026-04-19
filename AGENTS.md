# AI Agent Instructions for Smart Trucks Edge Stack

This file provides architectural context and strict coding guidelines for the FSI Smart Trucks edge telematics system. This repository does **not** follow the FSI Flask web application standard — it is an approved edge computing exception.

See [FSI_ECOSYSTEM.md](./FSI_ECOSYSTEM.md) for the full app portfolio and how this system connects to the rest of FSI infrastructure.

## 1. Project Overview

Smart Trucks is a containerized edge telematics stack deployed on Raspberry Pi hardware (Pi Zero 2 W / Pi 3B) via Balena. It collects GPS, BLE temperature/humidity, and power management data from vehicles and syncs to the shared FSI PostgreSQL database consumed by `kdnye/motive-dashboard`.

Three decoupled services run as Docker containers:
- **telematics-edge**: GPS (BerryGPS-IMU V4, NMEA over UART) + IMU harsh-event detection
- **ble-sensor**: Govee BLE temperature/humidity scanning
- **power-monitor**: UPS HAT battery SOC via INA219 I2C

## 2. Tech Stack

- **Runtime:** Python 3.10+, Docker containers on Balena
- **Edge persistence:** SQLite WAL (offline store-and-forward durability)
- **Cloud sync:** HTTPS POST to shared Cloud SQL PostgreSQL (via sync worker with retry/backoff)
- **Hardware I/O:** UART serial (GPS), I2C (IMU, INA219), BLE scan (bleak)
- **OTA updates:** Balena binary delta over cellular/Wi-Fi
- **No web framework:** This is not a Flask app. Do not add Flask or any HTTP server.

## 3. Strict Architectural Rules

### Decoupled Services

- Each container (telematics-edge, ble-sensor, power-monitor) is independent. Do not create cross-container function calls or shared mutable state.
- Use Docker Compose environment variable contracts for inter-service configuration.
- Each service owns its local SQLite WAL table. Sync workers handle upload independently.

### Offline Durability

- All sensor events must be written to local SQLite WAL before any sync attempt. Never write directly to the remote database in the hot path.
- The sync worker retries failed uploads with exponential backoff. Do not implement blocking network calls in sensor loops.
- If local SQLite WAL exceeds the configured size limit, emit a warning log and apply the drop-oldest eviction policy. Do not silently discard recent data.

### Hardware I/O Rules

- **GPS (UART):** Read NMEA sentences from the configured serial port (`GPS_SERIAL_PORT`). Parse with `pynmea2`. Never block the sensor loop on serial I/O for more than `GPS_READ_TIMEOUT` seconds.
- **IMU (I2C):** Access LSM9DS1 via the configured I2C bus (`IMU_I2C_BUS`). Harsh-event detection uses configurable acceleration threshold (`IMU_HARSH_EVENT_G`). Do not hard-code thresholds.
- **BLE:** Scan for Govee devices by MAC prefix. Scan interval is configurable (`BLE_SCAN_INTERVAL_S`). Never block the event loop waiting for a BLE scan to complete.
- **Power (INA219):** Poll via I2C at `POWER_POLL_INTERVAL_S`. SOC estimation uses `UPS_SHUNT_OHMS` — this value is hardware-specific and must not be changed without re-characterizing the UPS HAT.

### Power Mode State Machine

Power modes (normal → eco → critical → shutdown) are governed by battery SOC thresholds defined in environment variables (`POWER_ECO_SOC`, `POWER_CRITICAL_SOC`, `POWER_SHUTDOWN_SOC`). Do not hard-code SOC thresholds. State transitions must be logged before acting.

### Sync to Shared Database

- The sync worker posts to the FSI shared Cloud SQL PostgreSQL instance.
- **This app does not run Alembic migrations.** Table schema for fleet/telematics tables is owned by `kdnye/motive-dashboard`. This app only inserts rows.
- Never issue `ALTER TABLE`, `DROP COLUMN`, or `CREATE TABLE` from this codebase.
- Use `VEHICLE_ID` (set per-device in Balena device variables) in every telemetry row. Never hard-code a vehicle ID.

### Balena / OTA Rules

- Service configuration changes must be applied via Balena device or fleet variables, not by editing source files on the device.
- Do not add files to the Balena persistent volume that are not part of the defined schema — unexpected files will survive OTA updates and may cause state corruption.

## 4. Ecosystem Context

**Shared DB role:** Data producer only. Writes GPS, BLE, and power telemetry rows to tables owned by `kdnye/motive-dashboard`. Never runs structural DB migrations.

**Downstream consumer:** `kdnye/motive-dashboard` reads telematics data written by this system for the real-time fleet map and geofence tracking.

**Governance:** This system is an approved exception to the FSI Flask web application standard. It follows the FSI Python 3.10+, GCP Secret Manager, and UTC timestamp conventions where applicable.

Full ecosystem context: [FSI_ECOSYSTEM.md](./FSI_ECOSYSTEM.md)
