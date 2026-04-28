# Edge → Dashboard Data Contract

**Status:** design proposal (no code changes yet)
**Branch:** `claude/fix-dashboard-db-tables-nXkwz`
**Companion doc:** `kdnye/motive-dashboard` → `DASHBOARD_REBUILD_PLAN.md`

This file documents how the Pi fleet should produce and ship telemetry to the
shared FSI PostgreSQL instance owned by `kdnye/motive-dashboard`. It captures
the current state, the duplication that broke the dashboard, and the
single canonical contract we're moving to.

## 1. Current edge data flow (as of `main`)

Five containers run on each Pi via Balena (`docker-compose.yml`):

```
┌──────────────────┐  TCP 2947       ┌──────────────────┐  I2C/IMU
│ gps-multiplexer  │ ───────────────▶│ telematics-edge  │◀────── BerryGPS-IMU
│  (UART → TCP)    │                 │ (heartbeats,     │
└──────────────────┘                 │  GPS collector,  │
                                     │  maintenance)    │
┌──────────────────┐  I2C INA219     │                  │
│ power-monitor    │ ───────────────▶│  shared SQLite   │
│ (UPS HAT)        │                 │  /data/telematics│
└──────────────────┘                 │       .db        │
                                     │                  │
┌──────────────────┐  BLE scan       │                  │
│ ble-sensor       │ ───────────────▶│                  │
│ (Govee, beacons) │                 └────────┬─────────┘
└──────────────────┘                          │
                                              ▼
                                     ┌──────────────────┐
                                     │  sync-service    │  HTTPS POST
                                     │  (60s loop)      │ ────────────▶ WEBHOOK_URL
                                     └──────────────────┘
```

`telematics-edge/db.py::init_db()` creates **eight** tables in the shared
SQLite WAL:

| Table | Producer | Drained by `sync-service`? | Notes |
|---|---|---|---|
| `gps_points` | `gps_collector_worker` (`insert_gps_point`) | ❌ no | Rich payload with `local_sequence`, `payload_json`, `attempt_count`, `sent_at_utc` |
| `heartbeats` | `heartbeat_builder_worker` + `maintenance_worker` (`insert_heartbeat`) | ❌ no | Full `edge_telematics_heartbeat` and `edge_health` payloads as JSON |
| `edge_health` | `maintenance_worker` (`record_edge_health`) | ❌ no | Structured columns + payload JSON |
| `power_readings` | `power-monitor/main.py` | ❌ no | INA219 voltage/current/SoC; read in-process by `get_latest_power_snapshot` |
| `wake_signals` | `ble-sensor` (key beacon) | ❌ no | Parked-mode wake triggers, consumed locally |
| `local_gps` | `_insert_local_gps_point` (in `telematics-edge/main.py` and `parked_scan_worker`) | ✅ yes | Thin stub: `lat, lon, speed, fix_status` |
| `local_power` | (no producer) | ✅ yes | Always empty in production |
| `local_ble` | `ble-sensor` legacy path | ✅ yes | Thin: `mac_address, rssi, device_type` |

The outbound payload assembled by `sync-service/main.py::_build_payload`:

```json
{
  "vehicle_id": "TRK-905",
  "event_type": "unified_heartbeat",
  "gps":   { "id": ..., "lat": ..., "lon": ..., "speed": ..., "fix_status": ... },
  "power": {},
  "ble_scans": [ ... ]
}
```

Headers: `X-API-Key: $MOTIVE_API_KEY`. Endpoint: `WEBHOOK_URL`.

## 2. Why the dashboard saw nothing

Three independent failures stack:

1. **`sync-service` drains the wrong tables.** It empties `local_gps` /
   `local_power` / `local_ble` and ignores the rich `gps_points` /
   `heartbeats` / `edge_health` rows that the rest of the codebase fills
   with proper timestamps, `local_sequence` idempotency keys, IMU snapshots,
   queue depth, alerts, and `location_status.fix_status`.
2. **`local_power` has no producer.** `power-monitor/main.py` writes to
   `power_readings` (rich) and `telematics-edge` reads from there via
   `get_latest_power_snapshot()`. Nothing ever inserts into `local_power`,
   so the `power: {}` field on every outbound payload is empty.
3. **`unified_heartbeat` is not the cloud worker's primary contract.**
   `motive-dashboard/edge-telematics-worker/main.py` accepts both
   `unified_heartbeat` and `edge_telematics_heartbeat`, but its
   `_heartbeat_view()` is shaped for the rich payload (it looks for
   `gps.fix_status`, `power.state_of_charge_pct_estimate`,
   `power.bus_voltage_v`, IMU metrics, etc.). The thin `local_*` rows
   project poorly: `power_state` lands but `battery_percent` does not, and
   the worker's "is the fix locked?" logic falls through.

Combined effect: the Pi sends low-information rows; the cloud projects them
into `fleet_status_monitor.pi_*` columns; the Streamlit live map reads
`fleet_status_monitor.last_*` (Motive-owned); Pi GPS never appears.

## 3. Canonical edge contract — one truth, one event

### 3.1 Tables we keep

- `gps_points` — every fix `_is_valid_fix()` returns true for
- `heartbeats` — `edge_telematics_heartbeat` + `edge_health` (rich)
- `edge_health` — structured columns for triage queries
- `power_readings` — INA219 snapshots (already correct)
- `wake_signals` — local-only, not synced

### 3.2 Tables we drop

- `local_gps` — duplicate of `gps_points`
- `local_power` — duplicate of `power_readings`, also unused
- `local_ble` — duplicate of `ble_scans` block already embedded in heartbeats

### 3.3 Sync algorithm

`sync-service` becomes a single drain over the rich tables, ordered by
`captured_at_utc` ascending, batched up to `SYNC_BATCH_SIZE` (default 50):

1. `SELECT id, payload_json FROM heartbeats WHERE sent_at_utc IS NULL ORDER BY captured_at_utc ASC LIMIT N`
2. `SELECT id, payload_json FROM gps_points WHERE sent_at_utc IS NULL ORDER BY captured_at_utc ASC LIMIT N`
3. POST `{ "events": [ ...payloads ] }` to `WEBHOOK_URL`
4. On HTTP 2xx → `UPDATE ... SET sent_at_utc = NOW() WHERE id IN (...)`
5. On HTTP 4xx → log + increment `attempt_count`; do not retry
6. On HTTP 5xx / network → leave `sent_at_utc IS NULL`; exponential backoff
7. `purge_old_sent_rows()` keeps `sent_at_utc IS NOT NULL` rows for 7 days
   (3 days when disk free < 250 MB) — already implemented

Idempotency key (already declared in `motive-dashboard/ARCHITECTURE.md` §
"Edge Telematics Store-and-Forward"):

```
vehicle_id + captured_at_utc + local_sequence
```

The cloud worker derives a SHA-256-prefixed key when not provided
(`_derive_idempotency_key`). We will start providing it explicitly in the
`heartbeat_builder_worker` payload so replays are exactly-once at the
`iot_raw_payloads` layer.

### 3.4 Canonical wire format

Single event type: `edge_telematics_heartbeat`. The schema below matches the
worker's `_heartbeat_view` and `_collect_ble_detections` exactly.

```json
{
  "event_type": "edge_telematics_heartbeat",
  "vehicle_id": "TRK-905",
  "captured_at_utc": "2026-04-28T17:51:54Z",
  "idempotency_key": "TRK-905:edge_telematics_heartbeat:2026-04-28T17:51:54Z:0001",
  "process_uptime_sec": 8421,
  "location": {
    "fix_status": "locked",
    "device": "/dev/serial0",
    "latitude": 41.9616654,
    "longitude": -88.0260630,
    "altitude_m": 230.4,
    "speed_kmh": 100.6,
    "gps_timestamp": "2026-04-28T17:51:54Z",
    "lat": 41.9616654,
    "lng": -88.0260630,
    "lon": -88.0260630,
    "speed_knots": 54.3
  },
  "gps": { "...same as location for backward compat..." },
  "location_status": {
    "fix_status": "locked",
    "last_fix_age_sec": 2
  },
  "power_metrics": {
    "status": "ok",
    "voltage_v": 4.12,
    "bus_voltage_v": 4.12,
    "current_ma": -340,
    "state_of_charge_pct_estimate": 78.4,
    "is_charging": false,
    "source": "power_snapshot_db",
    "snapshot_found": true,
    "snapshot_stale": false,
    "snapshot_age_sec": 3,
    "snapshot_captured_at_utc": "2026-04-28T17:51:51Z"
  },
  "imu_metrics": {
    "status": "ok",
    "accel_g": [0.01, -0.02, 1.00],
    "gyro_dps": [0.1, 0.0, -0.1],
    "latest_harsh_event": null
  },
  "ble_scans": [
    {
      "timestamp_utc": "2026-04-28T17:51:30Z",
      "captured_at_utc": "2026-04-28T17:51:30Z",
      "sensors": [
        { "mac_address": "A4:C1:38:11:22:33", "rssi": -67, "name": "GVH5075", "temp_c": 4.1, "humidity_pct": 62.0, "battery_pct": 88 }
      ]
    }
  ],
  "queue": {
    "pending_gps_points": 0,
    "pending_heartbeats": 0,
    "queue_depth": 0,
    "gps_total": 14502,
    "heartbeats_total": 1733
  },
  "wifi_connected": true
}
```

`edge_health` events ride the same wire under `event_type=edge_health` and
keep the existing column-projecting shape consumed by
`_insert_edge_health_event`.

`unified_heartbeat`, `ble_sensor_scan` continue to be **accepted** by the
ingest endpoint (backward compat for older Pis) but are no longer **emitted**
by current edge builds. After all fleet devices are on the new build, those
event types can be dropped from `edge-telematics-api` / `edge-telematics-ingest-fn`.

## 4. Required edge code changes (separate PR)

1. `telematics-edge/db.py::init_db` — drop the `CREATE TABLE` statements for
   `local_gps`, `local_power`, `local_ble`. Keep migration logic that
   `DROP`s them on first boot if present (preserve unsent rows by
   re-inserting into `gps_points` with `payload_json` first).
2. `telematics-edge/main.py::_insert_local_gps_point` — remove. The rich
   `gps_collector_worker` is already calling `insert_gps_point()`.
3. `telematics-edge/main.py::heartbeat_builder_worker` — set
   `heartbeat_payload["idempotency_key"]` explicitly using
   `vehicle_id + captured_at_utc + local_sequence`.
4. `ble-sensor/main.py` — stop writing to `local_ble`; instead emit BLE blocks
   inside the next heartbeat (already half-implemented via the
   `ble_scans` array).
5. `sync-service/main.py` — rewrite to drain `gps_points` + `heartbeats` +
   `edge_health` ordered by `captured_at_utc`, batched, marking `sent_at_utc`
   on 2xx. Emit `event_type = edge_telematics_heartbeat` (or `edge_health`
   for the maintenance rows).

`AGENTS.md` rule "This app does not run Alembic migrations" stays intact;
the SQLite schema is local to the device.

## 5. Required cloud changes (in `motive-dashboard`, separate PR)

Tracked in `motive-dashboard/DASHBOARD_REBUILD_PLAN.md`. Critical-path items:

- Add `vw_fleet_live_position` SQL view that COALESCEs Pi data over Motive
  data with Pi winning when `pi_last_seen_at >= NOW() - INTERVAL '10 minutes'`.
- Repoint `app.py` and `pages/live_fleet.py` reads to that view.

Without that, fixing the edge contract still leaves Pi GPS invisible on the
map.

## 6. Desired outcomes

- A parked Pi with a fresh fix shows up gold on the live map within one
  heartbeat interval (60 s default).
- BLE temperature/humidity readings from Govee tags show up on the BLE asset
  page within one scan cycle.
- Battery state shows up on the Edge Health and Power Monitor pages without
  the `local_power` middleman.
- `iot_raw_payloads.idempotency_key` is dense (≥99% of rows have an explicit
  key from the edge), so replays after a device reboot are exactly-once and
  diagnostics queries don't double-count.
- The edge SQLite schema has one table per concept. New engineers reading
  `db.py` see the same names that appear in cloud queries
  (`gps_points`, `heartbeats`, `power_readings`, `edge_health`).

## 7. Design goals

1. **The producer owns durability.** SQLite WAL + `sent_at_utc` is the only
   exactly-once mechanism that survives multi-day connectivity outages.
2. **The wire format is the contract.** `edge_telematics_heartbeat` is one
   schema, owned jointly by this repo and `motive-dashboard`. Schema changes
   require a PR in both.
3. **The cloud table is owned by the consumer.** This repo never runs
   `ALTER TABLE`. `motive-dashboard` owns telematics tables (per
   `kdnye/fsi-docs/FSI_ECOSYSTEM.md` Schema Ownership table).
4. **Edge keeps its architectural exception.** Per the FSI governance
   handbook, smart-trucks is an approved non-Flask edge stack. Flask
   migration discussions in the dashboard rebuild do not change this.
5. **No new tables without dropping their duplicate.** When a new field
   appears, extend `payload_json` first, promote to a column only after a
   read consumer needs it.

## 8. Open questions for sign-off

1. **Migration strategy for in-flight `local_*` rows on existing Balena
   devices.** Option A: one-time script in the new build that copies unsent
   `local_gps` rows into `gps_points` before dropping the table. Option B:
   accept loss of pre-rollout queued rows (the fleet is currently down, so
   the queue is small). Recommend B for simplicity.
2. **BLE wire shape.** The cloud worker accepts both flat detection objects
   and `{ sensors: [...] }` blocks. Edge should consistently emit the
   `sensors[]` form going forward.
3. **Wake-signal events.** Today they're SQLite-local. Should harsh-event
   wakes also project to the cloud as a distinct event type for safety
   triage? Out of scope for this rebuild but flagged.
