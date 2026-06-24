# Edge Rebuild Record — Teardown & Restore

> **Purpose.** The Pi edge stack was stripped to a **base model** (location + Bluetooth
> presence reporting). This file records **what was removed/deferred and why**, the
> **purpose of each piece**, and **every table definition + column name**, so any piece can
> be restored on its own later.
>
> **This is a record, not the source of truth.** The live DDL lives in code (cited per
> table below). If you change a `CREATE TABLE` in a cited file, update this file too.

---

## 1. How to use this file

- To restore a removed/deferred capability, find it in §3, follow its **Restore** steps,
  and use the verbatim DDL (§4) / compose block (§5) it points to.
- The cloud already accepts everything the Pi can send; §6 maps each event to its Postgres
  tables and dashboard page so you can confirm a restored piece end-to-end.

## 2. Current base-model stack (`docker-compose.yml`)

| Service | Status | Purpose |
|---|---|---|
| `gps-multiplexer` | active | Sole owner of the serial GPS; serves NMEA over TCP `:2947`. |
| `telematics-edge` | active | Reads GPS (TCP) + IMU; writes GPS/heartbeat/health to shared SQLite. |
| `ble-sensor` | active | Scans BLE; records observations locally **and enqueues `ble_sensor_scan` for upload**. |
| `sync-service` | active | Store-and-forward uploader: drains shared-DB queues → cloud ingest. |
| `imu-calibration` | dormant | `restart: no`, `ENABLE_CALIBRATION=false`; one-shot calibration tool. |
| `ble-calibration` | dormant | `restart: no`, `ENABLE_CALIBRATION=false`; one-shot calibration tool. |
| `health` | active | Lightweight gunicorn health endpoint. |

## 3. Removed / deferred pieces

| Piece | Status | Purpose | Why removed/deferred | Restore |
|---|---|---|---|---|
| **power-monitor** | removed | INA219/UPS battery state-of-charge, brownout-safe shutdown, and BLE battery-saver input. | Simplify base model; was running in dummy mode; reduce RAM/CPU on the Pi Zero 2 W (512 MB). | Paste the compose block from **§5** back into `docker-compose.yml`; set `BLE_BATTERY_SAVER_ENABLED=true` on `ble-sensor`. The `power_readings` table (DDL in **§4.3**) is recreated by the service on boot. Consumers already degrade gracefully, so no other code change is needed. |
| **IMU / 10-DOF pipeline** | parked | Harsh-event detection + in-motion detection (requires calibration). | Base-model first; needs calibration work before it's useful. | `telematics-edge` still reads the IMU and emits `imu_metrics` in each heartbeat — it lands **only** as raw JSON in `iot_raw_payloads` (Raw Telemetry page). To surface it: add a structured IMU table + extraction in `motive-dashboard/edge-telematics-worker/main.py` + a dashboard view. |
| **EDGE_INGEST_KEY rename** | done | Decouple the Pi's ingest auth secret from the misleading `MOTIVE_API_KEY` name. | Cutover finished after `EDGE_INGEST_KEY` was set on every Balena device and on the cloud ingest function; `sync-service` now reads `EDGE_INGEST_KEY` only and `docker-compose.yml` no longer passes `MOTIVE_API_KEY`. | n/a — see commit history if a revert is needed. |

> Note: `power_monitor_ok=false` and `power_metrics.status="absent"` in heartbeats are
> expected after power-monitor removal (graceful degradation in `telematics-edge` and
> `ble-sensor`), not errors.

## 4. Local SQLite schema (verbatim from code)

### 4.1 Shared DB `/data/telematics.db`
Created by `telematics-edge/db.py` `init_db()`; the `ble_scans` queue is co-created there
and also by `ble-sensor/main.py` `_init_shared_queue_store()` (both idempotent).

```sql
-- source: telematics-edge/db.py:init_db
CREATE TABLE IF NOT EXISTS gps_points (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id        TEXT NOT NULL,
    captured_at_utc   TEXT NOT NULL,
    lat               REAL,
    lon               REAL,
    speed_kmh         REAL,
    fix_status        TEXT NOT NULL,
    source_device     TEXT,
    trip_id           TEXT,
    local_sequence    INTEGER NOT NULL,
    sent_at_utc       TEXT,                 -- NULL = pending upload; set on 2xx
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    payload_json      TEXT NOT NULL         -- the event POSTed to the cloud
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id        TEXT NOT NULL,
    heartbeat_type    TEXT NOT NULL,        -- "heartbeat" | "edge_health"
    captured_at_utc   TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    sent_at_utc       TEXT,
    attempt_count     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edge_health (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at_utc          TEXT NOT NULL,
    last_gps_fix_utc         TEXT,
    last_upload_success_utc  TEXT,
    queue_depth              INTEGER NOT NULL,
    disk_free_mb             REAL,
    wifi_state               TEXT,
    process_state            TEXT,
    payload_json             TEXT NOT NULL,
    sent_at_utc              TEXT,
    attempt_count            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS wake_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_type  TEXT NOT NULL,             -- e.g. "ble_key_beacon"
    created_at   TEXT NOT NULL
);

-- BLE store-and-forward upload queue (base-model addition)
-- source: telematics-edge/db.py:init_db  AND  ble-sensor/main.py:_init_shared_queue_store
CREATE TABLE IF NOT EXISTS ble_scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at_utc TEXT NOT NULL,
    payload_json    TEXT NOT NULL,          -- full ble_sensor_scan event (incl. sensors[].metadata)
    sent_at_utc     TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_gps_points_pending  ON gps_points(sent_at_utc, captured_at_utc);
CREATE INDEX IF NOT EXISTS idx_heartbeats_pending  ON heartbeats(sent_at_utc, captured_at_utc);
CREATE INDEX IF NOT EXISTS idx_edge_health_pending ON edge_health(sent_at_utc, captured_at_utc);
CREATE INDEX IF NOT EXISTS idx_ble_scans_pending   ON ble_scans(sent_at_utc, captured_at_utc);
```

Migrations / cleanup in `init_db()`:
- `_ensure_column(edge_health, sent_at_utc TEXT)` and
  `_ensure_column(edge_health, attempt_count INTEGER NOT NULL DEFAULT 0)` — upgrades pre-
  store-and-forward DBs (sync-service requires these columns).
- `DROP TABLE IF EXISTS local_gps; local_power; local_ble;` — legacy tables, removed.

Retention: `telematics-edge/db.py` `purge_old_sent_rows(days=7)` deletes rows where
`sent_at_utc IS NOT NULL AND captured_at_utc < cutoff` from `gps_points`, `heartbeats`,
`edge_health`, and `ble_scans` (wrapped in one `BEGIN IMMEDIATE`/`COMMIT`).

### 4.2 Local DB `/data/ble-sensor.db`
Created by `ble-sensor/main.py` `_init_local_store()`. Feeds the on-device triangulation
logic (`_resolve_tracked_asset_positions`); **not uploaded** directly.

```sql
-- source: ble-sensor/main.py:_init_local_store
CREATE TABLE IF NOT EXISTS ble_device_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at_utc TEXT NOT NULL,
    pi_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    tracked_mac_normalized TEXT NOT NULL,
    rssi INTEGER,
    latitude REAL,
    longitude REAL,
    gps_timestamp TEXT,
    gps_age_seconds INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ble_observations_mac_time
    ON ble_device_observations(tracked_mac_normalized, captured_at_utc DESC);

CREATE TABLE IF NOT EXISTS tracked_asset_registry (
    tracked_mac_normalized TEXT PRIMARY KEY,
    label TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tracked_asset_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at_utc TEXT NOT NULL,
    tracked_mac_normalized TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    resolved_pi_id TEXT,
    resolved_latitude REAL,
    resolved_longitude REAL,
    resolution_method TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    confidence_score REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tracked_asset_resolutions_lookup
    ON tracked_asset_resolutions(tracked_mac_normalized, captured_at_utc DESC);
```

### 4.3 REMOVED: `power_readings` (restore with power-monitor)
Was created in `/data/telematics.db` by `power-monitor/main.py`. Preserved here so the
producer and its consumers can be rebuilt.

```sql
-- source: power-monitor/main.py  (init_power_store)
CREATE TABLE IF NOT EXISTS power_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload TEXT NOT NULL                   -- JSON power snapshot (fields below)
);
CREATE INDEX IF NOT EXISTS idx_power_readings_latest
    ON power_readings(vehicle_id, occurred_at DESC, id DESC);
```

`payload` JSON fields written by `insert_power_reading()` (superset; dummy mode emits a
subset): `source`, `hardware_present`, `sensor_status_marker`, `status`/`read_status`,
`power_state` (`charging`/`discharging`/`battery_only`/`idle`/`sensor_fault`/
`external_input_present`), `state_of_charge_pct_estimate`, `voltage_v`, `bus_voltage_v`,
`shunt_voltage_mv`, `current_ma`, `power_mw`, `is_charging`, `battery_capacity_mah`,
`estimated_runtime_hours`, `sensor_latency_ms`, `read_latency_ms`, `dummy_mode`.

Consumers that read `power_readings` (read-only, already degrade to `None` when absent):
`telematics-edge/db.py` `get_latest_power_snapshot()` (heartbeat `power_metrics`) and
`ble-sensor/main.py` `_read_latest_power_from_telematics_db()` (battery-saver cadence).

### 4.4 Other services
`gps-multiplexer`, `health`, `imu-calibration`, `ble-calibration` keep **no persistent
SQLite schema** of their own (calibration containers mount `telematics_data:/data` and are
dormant by default).

## 5. REMOVED: power-monitor `docker-compose.yml` service block

Paste back verbatim under `services:` to restore. (Also re-add `power-monitor` to the
`/dev/i2c-1` consumers list mentally — it shares the I2C bus with `telematics-edge`.)

```yaml
  power-monitor:
    build:
      context: .
      dockerfile: power-monitor/Dockerfile
    privileged: true
    restart: always
    devices:
      - "/dev/i2c-1:/dev/i2c-1"     # Grants access to the UPS HAT
    volumes:
      - shared-sqlite:/data
    cpuset: "3"
    environment:
      - UDEV=1
      - VEHICLE_ID=${VEHICLE_ID}
      - DB_PATH=/data/telematics.db
      - TELEMATICS_DB_PATH=/data/telematics.db
      - UPS_BATTERY_CAPACITY_MAH
      - UPS_MAX_EXPECTED_AMPS
      - UPS_GAIN_STRATEGY
      - UPS_BUS_VOLTAGE_RANGE_V
      - POWER_SAMPLE_INTERVAL_SECONDS=${POWER_SAMPLE_INTERVAL_SECONDS:-2}
      - POWER_SAMPLE_INTERVAL_IDLE_SECONDS=${POWER_SAMPLE_INTERVAL_IDLE_SECONDS:-20}
      - POWER_FAST_SAMPLE_SOC_MARGIN_PCT=${POWER_FAST_SAMPLE_SOC_MARGIN_PCT:-15}
      - POWER_FAST_SAMPLE_VOLTAGE_MARGIN_V=${POWER_FAST_SAMPLE_VOLTAGE_MARGIN_V:-0.20}
      - POWER_MONITOR_MODE=${POWER_MONITOR_MODE:-dummy}
      - POWER_MONITOR_HARDWARE_REQUIRED=${POWER_MONITOR_HARDWARE_REQUIRED:-false}
      # Set UPS=no (per-device balena variable) for hardware with no UPS HAT,
      # e.g. solar/MPPT boards. power-monitor then idles instead of crash-looping.
      - UPS=${UPS:-yes}
```

When restored, flip `ble-sensor`'s `BLE_BATTERY_SAVER_ENABLED` back to `true` to re-enable
battery-aware scan cadence.

## 6. Cloud table reference map
Source: `motive-dashboard/edge-telematics-worker/main.py` + dashboard pages. Each accepted
event is also stored raw in `iot_raw_payloads` (→ Raw Telemetry page).

| Pi event_type | Postgres tables | Dashboard page |
|---|---|---|
| `edge_telematics_heartbeat` | `pi_heartbeat_events`, `edge_telemetry_history`, `fleet_status_monitor`, `edge_device_registry` | Pi Asset Viewer / Pi Asset History |
| `ble_sensor_scan` | `ble_asset_sightings`, `inventory` (auto-enroll via `_upsert_ble_inventory_asset`) | BLE Sensors, Pi Asset Viewer / History |
| `edge_health` | `edge_health_events` (view `edge_health_latest`) | Edge Health |
| (IMU `imu_metrics`) | not persisted to a structured table — raw JSON only | Raw Telemetry |

**Temp/humidity (e.g. Govee):** transmitted in the BLE advertisement `manufacturer_data`
carried in `ble_sensor_scan` `sensors[].metadata`. The **BLE Sensors** page decodes it from
`iot_raw_payloads`. Per-asset temp/humidity on **Pi Asset History** requires the worker to
persist `sensors[].metadata` into `ble_asset_sightings.payload` (see the base-model Part D
change).

## 7. Maintenance

Update this file whenever a `CREATE TABLE` / index changes in any cited source file, or when
a service is added/removed from `docker-compose.yml`. It is a restore record, not the schema
authority — code wins on any discrepancy.
