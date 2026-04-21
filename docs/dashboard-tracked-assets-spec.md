# Dashboard-side Tracked Asset Model + Admin API/UI Spec

This spec is intentionally for the **dashboard repository / shared DB owner** (`kdnye/motive-dashboard`), not for the edge hot path in this repo.

## Goals

- Keep BLE tracked assets as first-class entities, separate from Motive truck entities.
- Support CRUD-style admin operations (create/update/deactivate) for tracked assets.
- Match BLE scan events against tracked assets using canonical MAC normalization.
- Store latest observation fields for quick fleet map/status rendering.

## Data model (dashboard-side DB)

```sql
CREATE TABLE tracked_assets (
    asset_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label TEXT NOT NULL,
    ble_mac_raw TEXT,
    ble_mac_normalized TEXT NOT NULL,
    ble_mac_hash TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    confidence_score NUMERIC(5,4) NOT NULL DEFAULT 0.0000,
    last_seen_at_utc TIMESTAMPTZ,
    last_seen_lat DOUBLE PRECISION,
    last_seen_lon DOUBLE PRECISION,
    last_seen_vehicle_id TEXT,
    last_seen_pi_id TEXT,
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT tracked_assets_ble_mac_normalized_unique UNIQUE (ble_mac_normalized)
);

CREATE INDEX tracked_assets_active_idx ON tracked_assets (active);
CREATE INDEX tracked_assets_last_seen_idx ON tracked_assets (last_seen_at_utc DESC);
```

### Field intent

- `asset_id`: stable external identifier used in API/UI routes.
- `label`: human-editable display name (`"Cooler #7"`, `"Spare Pallet Beacon"`).
- `ble_mac_raw`: optional source MAC exactly as entered or first observed.
- `ble_mac_normalized`: canonical MAC for deterministic matching (`AA:BB:CC:DD:EE:FF`).
- `ble_mac_hash`: optional privacy-preserving lookup key when only hash is present in telemetry.
- `active`: soft-delete/deactivate toggle.
- `last_seen_*`: latest observation snapshot for fast admin/table views.
- `confidence_score`: 0.0-1.0 score owned by dashboard logic (e.g., seen count recency heuristic).

## Separation from Motive trucks

- Continue using existing truck/vehicle tables for Motive truck entities.
- `tracked_assets` should **not** re-use truck primary keys.
- If relationship context is needed, link by nullable foreign key in a join table (e.g., `tracked_asset_vehicle_affinity`) rather than merging truck and asset records.

## Canonical MAC normalization requirement

Apply this normalization in dashboard API before create/update and before match joins:

1. Strip all non-hex characters.
2. Validate resulting length is exactly 12 hex chars.
3. Uppercase.
4. Re-format into colon-delimited octets (`AA:BB:CC:DD:EE:FF`).

Reject invalid input with `400` and a clear validation message.

## Admin API (dashboard-side)

- `POST /api/admin/tracked-assets`
  - Create tracked asset.
  - Body: `label`, one of `ble_mac_raw | ble_mac_normalized`, optional `ble_mac_hash`, optional `confidence_score`.
- `PATCH /api/admin/tracked-assets/:assetId`
  - Update label, MAC fields, confidence, or active flag.
- `POST /api/admin/tracked-assets/:assetId/deactivate`
  - Convenience endpoint for soft deactivation.
- `GET /api/admin/tracked-assets`
  - List with filters (`active`, search by label/MAC, pagination).
- `GET /api/admin/tracked-assets/:assetId`
  - Detail record including last-seen snapshot.

### Security + integrity controls

- Require admin-role auth for all tracked-asset admin endpoints.
- Enforce unique constraint on normalized MAC.
- Log actor + change delta in audit trail table.
- Do not expose raw unhashed MAC to non-admin consumers.

## Admin UI behavior

- Dedicated **Tracked Assets** admin page (not mixed into truck settings).
- Form-level MAC validation and auto-normalization preview.
- Editable `label`, `active`, `confidence_score`, MAC fields.
- Table columns include:
  - Asset label / ID
  - Active status
  - Last seen timestamp
  - Last seen position
  - Last seen vehicle and Pi
  - Confidence score

## Edge compatibility note

The edge `ble-sensor` service in this repository now emits canonical uppercase-colon `mac_address` values in payloads, which aligns matching logic with this dashboard-side model.
