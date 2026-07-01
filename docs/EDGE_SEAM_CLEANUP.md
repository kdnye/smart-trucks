# Edge ↔ Dashboard Seam — Cleanup Backlog (edge side)

> Companion doc: `motive-dashboard/docs/EDGE_SEAM_CLEANUP.md` holds the cloud-side
> projects. This file covers the projects owned by **smart-trucks** (the edge).

## Seam overview / diagnosis

Two repos meet at one HTTP + Pub/Sub boundary:

- **smart-trucks** (edge/producer): a Raspberry Pi collects GPS, power (INA219),
  IMU, and BLE scans and stores-and-forwards them over HTTP.
- **motive-dashboard** (cloud/consumer): an ingest function validates and republishes
  events onto the Pub/Sub topic `edge-telematics-events`; worker functions project
  them into PostgreSQL; Streamlit reads the refined tables.

**The transport seam itself is healthy.** `sync-service` POSTs `{"events":[...]}` with
`X-API-Key: $EDGE_INGEST_KEY` to `WEBHOOK_URL` (`sync-service/main.py:149-164`); the
cloud checks the key and each event's `event_type`. Auth, batching, idempotency keys,
store-and-forward, and the GPS/heartbeat payloads all line up.

What's missing on the edge side is a **dead second representation of `edge_health`**
(which also starves several cloud columns) and an **un-synced high-resolution power
stream**. Each is written up below as a self-contained project.

### Non-issues (already checked — do not "fix" these)
- **GPS speed:** `build_location_payload` sends `speed_knots` alongside `speed_kmh`;
  the cloud reads it. Fine.
- **Power battery/state field names:** the edge sends `state_of_charge_pct_estimate`
  and `status`; the cloud maps them to `battery_percent`/`power_state` via fallback.
  Works (implicitly) — a rename is optional polish, not a fix.
- **Transport/auth/idempotency:** the `X-API-Key` header, the `{"events":[...]}`
  envelope, priority beacon, and idempotency-key format are all consistent with the
  cloud ingest.

---

## Project 3 — Kill the dead `edge_health` representation and fill the gaps

**Problem.** The edge emits `edge_health` **twice**, and only one copy is usable:

1. **Live/good path** — `maintenance_worker` builds `edge_payload` with
   `event_type:"edge_health"` and the full field set (`last_gps_fix_age_sec`,
   `last_locked_gps_point_age_sec`, `pending_gps_points`, `pending_heartbeats`,
   `wifi_connected`, `gps_reader_ok`, `power_monitor_ok`, `disk_free_mb`,
   `process_uptime_sec`) and stores it in the `heartbeats` table. This syncs and the
   cloud accepts it.
2. **Dead path** — the same cycle also writes a *reduced* dict into a separate local
   `edge_health` SQLite table via `record_edge_health`. That dict has **no
   `event_type`** and uses different names (`last_gps_fix_utc`, `wifi_state` string,
   `process_state`, `queue_depth`, `last_upload_success_utc=None`). Because
   `edge_health` is in `SYNC_TABLES`, `sync-service` POSTs these rows too — but the
   cloud **rejects** them (missing `event_type`), after which the edge marks them sent
   and purges them. Pure wasted bandwidth + a confusing duplicate schema.

Downstream, the cloud's `edge_health_events` table therefore never receives
`uploader_ok`, `last_upload_success_age_sec`, `queue_depth`, or `process_state` (see
cloud Project 2) — the live payload doesn't carry them and the reduced payload is
discarded.

**Evidence.**
- Live payload: `telematics-edge/main.py:933-949`
- Insert into `heartbeats` as `edge_health`: `telematics-edge/main.py:951-956`
- Dead reduced payload + local table: `telematics-edge/main.py:957-968`,
  `telematics-edge/db.py:314-340`
- Drained regardless: `sync-service/main.py:37` (`SYNC_TABLES`)
- Cloud rejects on missing `event_type`; cloud columns left NULL:
  `motive-dashboard/heartbeat-worker-fn/main.py:244-278`

**Proposed fix.** Remove the local `edge_health` table and the `record_edge_health`
sync path (drop it from `SYNC_TABLES`), keeping only the live `heartbeats`-table
`edge_health` event. Then add the genuinely-missing fields to that live payload:
`queue_depth`, `process_state`, `uploader_ok`, and a real `last_upload_success_utc`
(currently hardcoded `None`) plus a derived `last_upload_success_age_sec`. Wiring a
real upload-success timestamp means `sync-service` recording its last successful POST
time where `maintenance_worker` can read it.

**Acceptance / verification.** After the change, exactly one `edge_health` event per
cycle leaves the Pi, the cloud accepts it, and every `edge_health_events` column
populates with no unexpected NULLs.

**Rough effort.** Medium — payload + DB changes on the edge, plus plumbing the
last-upload-success timestamp from `sync-service` into the heartbeat state.

---

## Project 5 — Sync the high-resolution `power_readings` stream (optional)

**Problem.** The edge maintains an INA219 `power_readings` table, but `SYNC_TABLES`
omits it, so power reaches the cloud only as the single `power_metrics` snapshot
embedded in each ~60s heartbeat. The dashboard's Power Monitor page can therefore never
show sub-heartbeat resolution, and any fine-grained power history is lost if the device
is reset.

**Evidence.**
- `sync-service/main.py:37` (`SYNC_TABLES = ("heartbeats", "gps_points", "edge_health",
  "ble_scans")` — no `power_readings`)
- `EDGE_DASHBOARD_CONTRACT.md` (power_readings noted as an *optional* durable table
  "if the cloud dashboard needs it")

**Proposed fix.** Decide whether high-resolution power history is a product
requirement. If yes: add `power_readings` to `SYNC_TABLES` with its own
`event_type` (e.g. `power_reading`), define a matching cloud ingest branch + Postgres
table, and use a small batch size like BLE. If no: leave as-is and document that power
is heartbeat-cadence by design.

**Acceptance / verification.** If implemented, power rows appear in the cloud at INA219
cadence and the Power Monitor page renders finer-grained history.

**Rough effort.** Medium — spans both repos (new event type, cloud ingest branch,
migration); this is the lowest-priority item and marked optional.
