# Smart Truck Telematics — Development Plan

**Project**: Smart Truck Telematics Fleet Management  
**Maintainer**: Dave Alexander  
**Branch**: `claude/outline-dev-plan-VM3bh`  
**Status**: Development / Testing Phase (Pi 3B)  
**Date**: 2026-04-11

---

## Overview

This plan outlines the phased completion of the three-service telematics stack. The `ble-sensor` service is operational. The two remaining services — `gps-telematics` and `sync-service` — need to be built out, along with shared infrastructure and production hardening.

```
Current State:
  [ble-sensor]       ✅ Implemented & deployed
  [gps-telematics]   ❌ Not implemented (commented out in docker-compose.yml)
  [sync-service]     ❌ Not implemented (commented out in docker-compose.yml)
```

---

## Phase 1 — GPS Telematics Service

**Goal**: Read raw NMEA sentences from the BerryGPS-IMU V4 over UART, parse location and IMU data, apply geofencing logic, and write events to a shared SQLite database.

### 1.1 UART / Serial Integration

- Map `/dev/ttyS0` (Pi 3B) or `/dev/ttyAMA0` (Pi Zero 2 W) into the container.
- Use `pyserial` for async-capable UART reads at 9600 baud (GPS default).
- Handle UART availability gracefully — log and retry if device not yet ready at startup.

### 1.2 NMEA Sentence Parsing

Parse the following sentence types using `pynmea2`:

| Sentence | Data Extracted |
|----------|---------------|
| `$GPRMC` | Latitude, longitude, speed (knots), heading, UTC time, fix validity |
| `$GPGGA` | Fix quality, number of satellites, altitude, HDOP |
| `$GPGSA` | Fix type (2D/3D), PDOP |
| `$GPVTG` | True/magnetic track, speed in km/h |

- Reject and log malformed sentences without crashing.
- Emit a structured `GpsReading` dataclass per valid fix.

### 1.3 IMU Data (BerryGPS-IMU V4)

The BerryGPS-IMU V4 exposes an LSM9DS1 (accel/gyro/mag) and LPS25H (barometric pressure) over I2C.

- Read accelerometer (g-force), gyroscope (°/s), magnetometer (µT), and barometric pressure (hPa).
- Derive heading from magnetometer with hard-iron calibration offset.
- Detect harsh-braking / harsh-acceleration events: threshold configurable via `HARSH_EVENT_G_THRESHOLD` (default `0.4g`).
- Write IMU snapshots at a separate cadence from GPS (e.g., every 1 s) to avoid overloading the bus.

**Libraries**: `smbus2` for I2C register reads; consider `adafruit-circuitpython-lsm9ds1` for higher-level access.

### 1.4 Geofencing Logic

- Accept one or more geofence definitions via environment variable `GEOFENCE_JSON` (array of GeoJSON `Polygon` or `Circle` objects).
- On each GPS fix, evaluate inside/outside status using `shapely` point-in-polygon.
- Emit `geofence_enter` and `geofence_exit` events with timestamp, vehicle ID, and fence ID.
- Support circular fences (centre lat/lon + radius in metres) via Haversine distance.

### 1.5 Speed & Trip Logic

- Track trip state: `idle`, `moving`, `stopped`.
- Transition to `moving` when speed > `SPEED_THRESHOLD_KMH` (default `5`) for 3+ consecutive fixes.
- Record trip start/end, total distance (odometer via summed Haversine segments), max speed, and average speed.
- Emit `trip_start` and `trip_end` events.

### 1.6 Local Persistence

- Write all events (`gps_fix`, `geofence_enter`, `geofence_exit`, `trip_start`, `trip_end`, `harsh_event`, `imu_snapshot`) to a shared SQLite database at `/data/telematics.db`.
- Use WAL journal mode to allow concurrent reads by the sync-service.
- Schema:
  ```sql
  CREATE TABLE events (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      vehicle_id  TEXT NOT NULL,
      event_type  TEXT NOT NULL,
      occurred_at TEXT NOT NULL,  -- ISO 8601 UTC
      payload     TEXT NOT NULL,  -- JSON blob
      synced      INTEGER DEFAULT 0
  );
  ```

### 1.7 Deliverables

```
gps-telematics/
├── Dockerfile
├── requirements.txt      # pyserial, pynmea2, smbus2, shapely, aiofiles
├── main.py               # async entry point, config loading
├── nmea_reader.py        # UART read loop + sentence parser
├── imu_reader.py         # I2C register reads for LSM9DS1 / LPS25H
├── geofence.py           # polygon/circle evaluation logic
├── trip_tracker.py       # speed state machine + odometer
└── db.py                 # SQLite schema init + write helpers
```

---

## Phase 2 — Sync Service

**Goal**: Drain the SQLite event queue and deliver payloads to the cloud webhook; handle intermittent connectivity gracefully; respect power and data budgets.

### 2.1 Queue Drain Loop

- At each `POLL_INTERVAL` (default 300 s), read up to `BATCH_SIZE` (default 50) unsynced rows from `events` table.
- Serialize rows into a single batched JSON payload per POST to reduce connection overhead.
- On HTTP 2xx, mark rows `synced = 1`.
- On HTTP 4xx (client error), mark rows as permanently failed (add `sync_failed` column, increment counter).
- On HTTP 5xx or network error, leave `synced = 0` and retry on next cycle.

### 2.2 Connectivity Detection

- Before attempting upload, check connectivity via a fast HEAD request to the webhook host (timeout 3 s).
- If offline, skip cycle and log — do not burn retry budget needlessly.
- Track consecutive offline cycles; emit a local `connectivity_loss` event after `OFFLINE_ALERT_THRESHOLD` (default 5) cycles.

### 2.3 Retry / Back-off

- Implement exponential back-off capped at `MAX_BACKOFF_SECONDS` (default 1800 s) for repeated 5xx failures.
- Reset back-off on first successful delivery.

### 2.4 Data Budget Awareness

- Track cumulative bytes uploaded per calendar day in a lightweight counter table.
- If `DAILY_DATA_BUDGET_MB` is set and approaching limit (>90%), switch to summary-only mode: drop `imu_snapshot` rows, send only `gps_fix`, geofence, and trip events.

### 2.5 BLE Payload Forwarding

- The `ble-sensor` service currently POSTs directly to a webhook. Refactor to instead write to the shared SQLite `events` table (event_type = `ble_scan`).
- The sync-service becomes the single egress point, simplifying credential management (one `WEBHOOK_URL`, one `API_KEY`).

### 2.6 Deliverables

```
sync-service/
├── Dockerfile
├── requirements.txt      # aiohttp, aiosqlite
├── main.py               # async entry point
├── queue_drain.py        # batch read + POST logic
├── connectivity.py       # online check + back-off state
└── db.py                 # shared read helpers (mirrors gps-telematics/db.py schema)
```

---

## Phase 3 — Power Management Integration

**Goal**: Monitor battery and solar state via the INA219 chip on the UPS HAT; implement low-power operating modes to extend mission duration.

### 3.1 INA219 Monitoring

- Read bus voltage (V), shunt voltage (mV), current (mA), and power (mW) via I2C using `pi-ina219`.
- Sample every 10 s and write to a `power_readings` table in SQLite.
- Derive state-of-charge estimate from voltage curve.

### 3.2 Power Modes

| Mode | Trigger | Behaviour |
|------|---------|-----------|
| `normal` | SoC > 40% | All services at full cadence |
| `eco` | SoC 20–40% | GPS poll every 30 s, BLE scan every 120 s, sync every 600 s |
| `critical` | SoC < 20% | GPS only (no BLE/IMU), sync every 1800 s |
| `shutdown` | SoC < 10% | Flush queue, write shutdown event, halt gracefully |

- Power mode is written to a shared environment/config file (or a `system_state` table) that other services watch.

### 3.3 Deliverables

- Add `power_monitor.py` to `gps-telematics` or as a standalone fourth service (`power-monitor`).
- Expose `POWER_MONITOR_ENABLED` env var (default `true`).

---

## Phase 4 — Shared Infrastructure & Docker Compose

### 4.1 Shared Volume

Add a named volume `telematics-data` mounted at `/data` across all services so SQLite is accessible to all containers:

```yaml
volumes:
  telematics-data:

services:
  ble-sensor:
    volumes:
      - telematics-data:/data

  gps-telematics:
    volumes:
      - telematics-data:/data

  sync-service:
    volumes:
      - telematics-data:/data
```

### 4.2 Uncomment & Wire gps-telematics and sync-service

```yaml
gps-telematics:
  build: ./gps-telematics
  privileged: true
  devices:
    - "/dev/ttyS0:/dev/ttyS0"  # Pi 3B UART
  restart: always
  environment:
    - VEHICLE_ID=${VEHICLE_ID}
    - GEOFENCE_JSON=${GEOFENCE_JSON}

sync-service:
  build: ./sync-service
  depends_on:
    - gps-telematics
  restart: always
  environment:
    - VEHICLE_ID=${VEHICLE_ID}
    - WEBHOOK_URL=${WEBHOOK_URL}
    - API_KEY=${API_KEY}
    - POLL_INTERVAL=300
```

### 4.3 Environment Variable Consolidation

Centralise all fleet-wide vars in Balena Dashboard and add the following new ones:

| Variable | Description | Default |
|----------|-------------|---------|
| `GEOFENCE_JSON` | GeoJSON array of fence definitions | `[]` |
| `TEMP_THRESHOLD` | Cold-chain alert trigger (°C) | `4.0` |
| `SPEED_THRESHOLD_KMH` | Trip start threshold | `5` |
| `HARSH_EVENT_G_THRESHOLD` | Harsh driving g-force trigger | `0.4` |
| `BATCH_SIZE` | Events per sync batch | `50` |
| `DAILY_DATA_BUDGET_MB` | Cellular data cap per day | `60` |
| `POWER_MONITOR_ENABLED` | Enable INA219 monitoring | `true` |
| `OFFLINE_ALERT_THRESHOLD` | Cycles before offline alert | `5` |

---

## Phase 5 — Testing & Validation

### 5.1 Unit Tests

- `test_nmea_parser.py` — Feed known-good and malformed NMEA strings; assert parsed fields.
- `test_geofence.py` — Assert enter/exit detection for polygon and circle fences.
- `test_trip_tracker.py` — Simulate speed sequences; assert state transitions.
- `test_queue_drain.py` — Mock HTTP server; assert batch delivery, retry on 5xx, skip on 4xx.
- `test_power_modes.py` — Assert mode transitions at voltage thresholds.

### 5.2 Integration Tests

- Spin up all three containers via `docker-compose up` against a mock webhook server (e.g., `httpbin` or a local FastAPI stub).
- Verify end-to-end flow: BLE scan → SQLite → sync → webhook receipt.
- Simulate connectivity loss; verify queue drains correctly when connectivity restores.

### 5.3 Hardware Validation (Pi 3B)

- Confirm `/dev/ttyS0` UART output at 9600 baud with BerryGPS-IMU V4 mounted.
- Confirm I2C addresses: `0x1E` (magnetometer), `0x6B` (accel/gyro), `0x5D` (barometer), `0x40` (INA219).
- Drive route test: verify trip events, geofence transitions, and BLE scan events all appear in the cloud dashboard.

### 5.4 Balena OTA Validation

- Test `balena push` to dev fleet on Pi 3B.
- Confirm binary delta update delivers only changed layers.
- Test graceful service restart after OTA without data loss (SQLite WAL integrity).

---

## Phase 6 — Production Hardening

### 6.1 Watchdog

- Enable Balena's built-in `BALENA_SUPERVISOR_OVERRIDE_LOCK` watchdog or add a lightweight process watchdog script that restarts a container if it has not written to SQLite in `WATCHDOG_TIMEOUT_SECONDS` (default 120).

### 6.2 Log Management

- Set `BALENA_APP_RESTART_POLICY` to avoid log accumulation filling the SD card.
- Use structured JSON logging (one JSON object per line) across all services for easy ingestion.
- Rotate SQLite database: archive and delete `synced = 1` rows older than `DATA_RETENTION_DAYS` (default 7).

### 6.3 Security

- Store `API_KEY` and `MAC_HASH_SALT` as Balena **Secrets**, not plain environment variables.
- Enforce HTTPS for all webhook URLs; reject HTTP at runtime.
- Validate `WEBHOOK_URL` scheme and hostname at startup; fail fast on misconfiguration.

### 6.4 Pi Zero 2 W Migration

- Confirm `/dev/ttyAMA0` (instead of `/dev/ttyS0`) device mapping in docker-compose for Pi Zero 2 W.
- Profile memory usage — target < 80 MB RSS across all three services combined on 512 MB RAM.
- Replace `shapely` with a pure-Python alternative (`polycircles`, manual Haversine) if ARM wheel is unavailable.

---

## Milestone Summary

| Milestone | Deliverable | Estimated Effort |
|-----------|-------------|-----------------|
| M1 | `gps-telematics` service (NMEA + IMU + geofence + SQLite) | Phase 1 |
| M2 | `sync-service` with queue drain + retry + connectivity detection | Phase 2 |
| M3 | Power management integration (INA219 + power modes) | Phase 3 |
| M4 | docker-compose wired, shared volume, env vars consolidated | Phase 4 |
| M5 | Unit + integration test suite passing | Phase 5 |
| M6 | Hardware route test on Pi 3B validated | Phase 5 |
| M7 | Production hardening (watchdog, log rotation, security) | Phase 6 |
| M8 | Pi Zero 2 W deployment validated | Phase 6 |

---

## Open Questions

1. **Webhook API contract** — What schema does the cloud dashboard expect? Confirm field names and whether batch envelopes are supported, or if individual events should be POSTed separately.
2. **Geofence source of truth** — Are geofences static per-vehicle (env var) or dynamic (fetched from dashboard API)? If dynamic, the sync-service needs a pull endpoint.
3. **Temperature alerts** — `TEMP_THRESHOLD` is defined but no temperature alert event type exists yet. Confirm whether Govee temperature readings should trigger an immediate out-of-band alert POST (bypassing the normal queue) when threshold is breached.
4. **Pi Zero 2 W readiness** — The Pi 3B is used for testing. Confirm target production hardware and any constraints (RAM, CPU) that affect library choices.
5. **Dashboard** — Is there an existing cloud dashboard or is one planned? Clarify whether a lightweight self-hosted option (e.g., Grafana + InfluxDB, or a simple Next.js app) is in scope.
