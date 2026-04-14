# Dashboard Handoff: Pi Telemetry Data Contract + Troubleshooting Plan

This summary is intended for the dashboard repository so UI work can proceed without re-reading the edge collector code.

## 1) Data streams emitted from the Pi stack

### A) `telematics-edge` heartbeat payload (GPS + IMU + power)

Event shape:

```json
{
  "event_type": "edge_telematics_heartbeat",
  "vehicle_id": "TRK-905",
  "timestamp_utc": "2026-04-13T12:34:56.000000+00:00",
  "uptime_seconds": 123456,
  "power_metrics": { ... },
  "imu_metrics": { ... },
  "location": { ... }
}
```

#### GPS datapoints (`location`)
- `fix_status`: `locked` or `searching`
- `device`: active serial device path (`/dev/serial0`, etc.) when locked
- `latitude`: decimal degrees
- `longitude`: decimal degrees
- `altitude_m`: altitude (if available from sentence)
- `speed_knots`: speed over ground in knots
- `gps_timestamp`: NMEA timestamp string (if available)

#### IMU datapoints (`imu_metrics`)
- `status`: `initializing` or `ok`
- `timestamp`: UTC timestamp for IMU sample
- `accel_x`, `accel_y`, `accel_z`: acceleration in g
- `magnitude_2d`: planar acceleration magnitude (x/y), used for harsh event detection
- `is_harsh_event`: boolean threshold result
- `latest_harsh_event`: optional object mirroring a harsh event snapshot

#### Power datapoints (`power_metrics`)
- `status`: inherited from latest `power_snapshot` record; `absent` when no snapshot row exists
- `voltage_v`: battery/bus voltage in volts (when available)
- `current_ma`: current in milliamps (when available)
- `power_mw`: power in milliwatts (when available)
- `state_of_charge_pct_estimate`: coarse battery SOC estimate derived from loaded voltage curve
- `estimate_method`: estimation strategy identifier (`voltage_curve_loaded`)
- `is_charging`: boolean (`current_ma > 0`)
- `message`: only present on read errors
- `source`: always `power_snapshot_db`
- `snapshot_found`: `true` when a row exists in `power_readings`
- `snapshot_stale`: `true` when latest row age exceeds edge freshness threshold
- `snapshot_age_sec`: age in seconds of the latest row, or `null` if unavailable
- `snapshot_captured_at_utc`: timestamp of the latest row used for heartbeat power metrics

> Dashboard contract note: migrate any field mappings from `state_of_charge_pct` to `state_of_charge_pct_estimate`.

---

### B) BLE scan payload (`ble-sensor`)

Event shape:

```json
{
  "vehicle_id": "TRK-905",
  "event_type": "ble_sensor_scan",
  "scan_duration_seconds": 15,
  "sensor_count": 2,
  "sensors": [ ... ]
}
```

Per-device datapoints (`sensors[]`):
- `device_id`: stable hashed device identifier (privacy-preserving default)
- `mac_address`: raw MAC (currently still included)
- `rssi`: received signal strength indicator
- `device_type`: enrichment label (`Apple`, `Samsung`, `Unknown`, etc.)
- `name`: optional local name when enabled
- `metadata.manufacturer_data`: company IDs + payload hex
- `metadata.service_uuids`: list of advertised service UUIDs
- `metadata.service_data`: service data payloads by UUID, hex encoded
- `metadata.tx_power`: advertised transmit power (if present)

## 2) Dashboard recommendations (raw troubleshooting page)

Create a dedicated page/tab: **"Raw Telemetry"**.

### Sections
1. **Latest Heartbeat (JSON)**
   - Pretty-printed payload.
   - Color chip for `location.fix_status`, `imu_metrics.status`, `power_metrics.status`.
2. **BLE Last Scan (JSON + device table)**
   - Full raw JSON.
   - Table columns: `device_id`, `mac_address`, `rssi`, `device_type`, `tx_power`.
3. **Field Health Checks**
   - Missing/nullable field warnings.
   - Timestamp staleness badges.
4. **Copy/Export**
   - Copy JSON to clipboard.
   - Download snapshot for incident debugging.

### Alerting notes
- Keep alerting transport abstract in UI/service layer; account-level email transport is controlled through **Postmark**.

## 3) Implementation priorities for the dashboard repo

1. Build reusable `RawJsonCard` component.
2. Add `TelemetryRawPage` with heartbeat + BLE panels.
3. Add `field diagnostics` helper that flags:
   - stale timestamps,
   - missing GPS lock,
   - no BLE detections,
   - IMU harsh events.
4. Add route guard (internal/admin view only).

## 4) Example API contract assumptions

Suggested API endpoints for dashboard consumption:
- `GET /api/vehicles/:vehicleId/telematics/latest`
- `GET /api/vehicles/:vehicleId/ble/latest`
- `GET /api/vehicles/:vehicleId/telematics/history?minutes=30`

If the dashboard receives events via websocket, keep an append-only in-memory ring buffer and retain the latest raw payload per event type for diagnostics rendering.
