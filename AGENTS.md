# AI Agent Instructions for Smart Trucks Edge Stack

This file provides architectural context and strict coding guidelines for the FSI Smart Trucks edge telematics system. This repository does **not** follow the FSI Flask web application standard — it is an approved edge computing exception.

See [FSI_ECOSYSTEM.md](./FSI_ECOSYSTEM.md) for the full app portfolio and how this system connects to the rest of FSI infrastructure.

## 1. Project Overview

Smart Trucks is a containerized edge telematics stack deployed on Raspberry Pi hardware (Pi Zero 2 W / Pi 3B) via Balena. It collects GPS, BLE temperature/humidity, and power management data from vehicles and syncs to the shared FSI PostgreSQL database consumed by `kdnye/motive-dashboard`.

Four decoupled services run as Docker containers:
- **gps-multiplexer**: Reads GPS from hardware serial and exposes it over TCP (port 2947)
- **telematics-edge**: Reads GPS via TCP + IMU via I2C; manages SQLite WAL and cloud sync
- **ble-sensor**: Govee BLE temperature/humidity scanning
- **power-monitor**: UPS HAT battery SOC monitoring via INA219 I2C

## 2. Tech Stack

- **Runtime:** Python 3.10+, Docker containers on Balena
- **Edge persistence:** SQLite WAL (offline store-and-forward durability)
- **Cloud sync:** HTTPS POST to shared Cloud SQL PostgreSQL (via sync worker with retry/backoff)
- **Hardware I/O:** UART serial (GPS via gps-multiplexer), I2C (IMU, INA219), BLE scan (bleak)
- **OTA updates:** Balena binary delta over cellular/Wi-Fi
- **No web framework:** This is not a Flask app. Do not add Flask or any HTTP server.

## 3. Strict Architectural Rules

### Decoupled Services

- Each container (telematics-edge, ble-sensor, power-monitor) is independent. Do not create cross-container function calls or shared mutable state.
- Use Docker Compose environment variable contracts for inter-service configuration.
- The `gps-multiplexer` container exposes the GPS device over TCP (`tcp://gps-multiplexer:2947`) so `telematics-edge` reads GPS without requiring direct serial access.

### Offline Durability

- All sensor events must be written to local SQLite WAL before any sync attempt. Never write directly to the remote database in the hot path.
- The sync worker retries failed uploads with exponential backoff. Do not implement blocking network calls in sensor loops.
- Purge policy is **age-based**: the maintenance worker runs `purge_old_sent_rows(days=3)` when disk free space drops below 250 MB, or `purge_old_sent_rows(days=7)` otherwise. Only rows already marked `sent_at_utc` are purged — unsent data is never deleted.

### Hardware I/O Rules

- **GPS:** Primary device path configured via `GPS_SERIAL_DEVICE` (e.g. `/dev/serial0`); additional probe candidates in `GPS_SERIAL_CANDIDATES`. The `telematics-edge` container connects via `tcp://gps-multiplexer:2947`. Sample interval: `GPS_SAMPLE_INTERVAL_SECONDS` (default 5s). Baud rate: `GPS_BAUD_RATE` (default 9600). Parsed by `NMEAReader`.
- **IMU (I2C):** Bus number via `IMU_I2C_BUS` (default 1). Expected I2C addresses via `IMU_EXPECTED_ADDRESSES`. Whether IMU must be present to start: `IMU_REQUIRED` (default `true`). Harsh-event thresholds are managed internally by `IMUReader` — there is no external threshold environment variable.
- **BLE (`ble-sensor`):** Scan duration is `SCAN_DURATION_SECONDS` (default 20s). Interval between scan cycles is `POLL_INTERVAL` (default 60s). MAC anonymization: `ANONYMIZE_MAC` (default `false`).
- **Power (INA219, `power-monitor`):** UPS configuration uses `UPS_BATTERY_CAPACITY_MAH` (default 4400), `UPS_MAX_EXPECTED_AMPS` (default 4.0), `UPS_GAIN_STRATEGY` (default `auto`), `UPS_BUS_VOLTAGE_RANGE_V` (default 32). These values are hardware-specific — do not change them without re-characterizing the UPS HAT.

### Power-Aware Sleep

Parked-mode sleep duration is adapted to battery SOC by `_adaptive_parked_sleep_seconds()`:
- SOC < 10% → 300s sleep
- SOC < 25% → 120s sleep
- Otherwise → 50s (default `PARKED_SLEEP_SECONDS`)

These thresholds are currently **hard-coded constants** in `telematics-edge/main.py`. Do not introduce additional hard-coded thresholds — any new power-management logic should read from environment variables.

### Sync to Shared Database

- The sync worker POSTs to the FSI shared Cloud SQL PostgreSQL instance.
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
