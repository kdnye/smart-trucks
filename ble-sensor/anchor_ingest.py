"""Anchor observation ingest: local MQTT -> ble-sensor's observation store.

Subscribes to the frames anchor-bridge republishes from the gateway ATOM
(``anchors/+/scan|status|gateway``) and folds them into the same pipeline the
Pi's own scanner feeds:

- ``ble_device_observations`` rows with the anchor's ``observer_id`` — the
  existing tracked-asset resolver then sees every vantage point at the site
  and can rank observers per asset (nearest-anchor presence).
- ``ble_scans`` store-and-forward queue entries (``ble_sensor_scan`` payloads
  with ``observer_id`` / ``parent_pi_id`` / ``anchor`` fields) so sync-service
  ships them to the cloud unchanged.
- ``anchor_status`` snapshot rows for edge-side diagnostics.

Registration/quarantine: if ``ANCHOR_REGISTRY_PATH`` (JSON, synced from the
dashboard's ble_anchor_registry) or ``ANCHOR_ALLOWLIST`` (comma-separated ids)
is configured, only listed-and-active anchors contribute observations; unknown
anchors are still tracked in ``anchor_status`` (registered=0) so they surface
for approval, but their scans are dropped. With neither configured the ingest
runs open (accepts all anchors) — the cloud side still auto-registers them
inactive for review.

Runs as a paho-mqtt network thread inside the ble-sensor process; all SQLite
writes go through shared.sqlite_util's lock-retry helpers. Import-light and
callback-driven so the message handling is unit-testable without a broker
(see tests/test_anchor_ingest.py).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from shared import sqlite_util
from shared.env import read_bool_env, read_int_env, read_str_env

logger = logging.getLogger(__name__)

# Throttle "unregistered anchor" warnings per anchor (seconds).
QUARANTINE_LOG_INTERVAL_SECONDS = 300.0


@dataclass(frozen=True)
class IngestConfig:
    enabled: bool
    mqtt_host: str
    mqtt_port: int
    registry_path: str
    allowlist: frozenset[str]
    registry_refresh_seconds: int


def load_ingest_config() -> IngestConfig:
    raw_allowlist = read_str_env("ANCHOR_ALLOWLIST", "")
    allowlist = frozenset(a.strip() for a in raw_allowlist.split(",") if a.strip())
    return IngestConfig(
        enabled=read_bool_env("ANCHOR_INGEST_ENABLED", default=True),
        mqtt_host=read_str_env("ANCHOR_MQTT_HOST", "127.0.0.1"),
        mqtt_port=read_int_env("ANCHOR_MQTT_PORT", 1883, minimum=1),
        registry_path=read_str_env("ANCHOR_REGISTRY_PATH", "/data/anchor_registry.json"),
        allowlist=allowlist,
        registry_refresh_seconds=read_int_env("ANCHOR_REGISTRY_REFRESH_SECONDS", 300, minimum=10),
    )


@dataclass
class IngestContext:
    """Everything the message handler needs, injected to stay testable and to
    avoid a circular import with ble-sensor's main module."""

    config: IngestConfig
    pi_id: str
    vehicle_id: str
    local_db_path: str
    # main.py's _record_scan_observations / _enqueue_ble_scan (payload dicts in).
    record_observations: Callable[[dict[str, Any]], None]
    enqueue_scan: Callable[[dict[str, Any]], None]
    # Synchronous Pi-location snapshot (main.py's cache/db readers).
    get_pi_location: Callable[[], dict[str, Any]]
    # OUI-based best-effort device classification (main.py's KNOWN_OUIS).
    classify_mac: Callable[[str], str]
    canonicalize_mac: Callable[[str], str]
    # Mutable registry cache: anchor_id -> active. None => open mode.
    registry: dict[str, bool] | None = None
    registry_loaded_at: float = 0.0
    _quarantine_logged_at: dict[str, float] = field(default_factory=dict)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_anchor_id(anchor_id: Any) -> bool:
    if not isinstance(anchor_id, str) or len(anchor_id) != 13:
        return False
    if not anchor_id.startswith("anchor-"):
        return False
    return all(ch in "0123456789abcdef" for ch in anchor_id[len("anchor-") :])


def load_anchor_registry(path: str, allowlist: frozenset[str]) -> dict[str, bool] | None:
    """Return anchor_id -> active, or None for open mode (nothing configured).

    File format (synced down from the dashboard's ble_anchor_registry):
        [{"anchor_id": "anchor-a1b2c3", "active": true, "label": "..."}, ...]
    The env allowlist is merged in as active=True (bootstrap without a file).

    Open mode is decided by *configuration*, not by the loaded result: an
    existing-but-empty registry file means "no anchor is approved here" and
    must quarantine everything — returning None for it would silently bypass
    the allowlist.
    """
    if not allowlist and not (path and os.path.exists(path)):
        return None  # nothing configured -> open mode
    registry: dict[str, bool] = {anchor_id: True for anchor_id in allowlist}
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as registry_file:
                raw = json.load(registry_file)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read anchor registry %s: %s", path, exc)
            raw = None
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                anchor_id = item.get("anchor_id")
                if valid_anchor_id(anchor_id):
                    registry[str(anchor_id)] = bool(item.get("active", True))
    return registry


def _refresh_registry(ctx: IngestContext) -> None:
    now = time.monotonic()
    if ctx.registry_loaded_at and now - ctx.registry_loaded_at < ctx.config.registry_refresh_seconds:
        return
    ctx.registry = load_anchor_registry(ctx.config.registry_path, ctx.config.allowlist)
    ctx.registry_loaded_at = now


def _anchor_allowed(ctx: IngestContext, anchor_id: str) -> bool:
    _refresh_registry(ctx)
    if ctx.registry is None:
        return True  # open mode
    return ctx.registry.get(anchor_id, False)


def _log_quarantined(ctx: IngestContext, anchor_id: str) -> None:
    now = time.monotonic()
    last = ctx._quarantine_logged_at.get(anchor_id, 0.0)
    if now - last < QUARANTINE_LOG_INTERVAL_SECONDS:
        return
    ctx._quarantine_logged_at[anchor_id] = now
    logger.warning(
        "Anchor %s is not registered/active for this Pi; recording status only "
        "and dropping its scans. Register it in the dashboard (BLE Assets -> "
        "Anchor Registry) or add it to ANCHOR_ALLOWLIST.",
        anchor_id,
    )


def _upsert_anchor_status(
    ctx: IngestContext,
    anchor_id: str,
    *,
    role: str,
    registered: bool,
    frame: dict[str, Any],
) -> None:
    def _op() -> None:
        conn = sqlite_util.connect(ctx.local_db_path, isolation_level="IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO anchor_status (
                    anchor_id, parent_pi_id, role, registered, last_seen_utc,
                    link_rssi, fw_version, free_heap, uptime_s,
                    report_interval_s, rssi_floor, channel
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(anchor_id) DO UPDATE SET
                    parent_pi_id = excluded.parent_pi_id,
                    role = excluded.role,
                    registered = excluded.registered,
                    last_seen_utc = excluded.last_seen_utc,
                    link_rssi = COALESCE(excluded.link_rssi, anchor_status.link_rssi),
                    fw_version = COALESCE(excluded.fw_version, anchor_status.fw_version),
                    free_heap = COALESCE(excluded.free_heap, anchor_status.free_heap),
                    uptime_s = COALESCE(excluded.uptime_s, anchor_status.uptime_s),
                    report_interval_s = COALESCE(excluded.report_interval_s, anchor_status.report_interval_s),
                    rssi_floor = COALESCE(excluded.rssi_floor, anchor_status.rssi_floor),
                    channel = COALESCE(excluded.channel, anchor_status.channel)
                """,
                (
                    anchor_id,
                    ctx.pi_id,
                    role,
                    1 if registered else 0,
                    _utc_now_iso(),
                    frame.get("link_rssi"),
                    frame.get("fw"),
                    frame.get("free_heap"),
                    frame.get("uptime_s"),
                    frame.get("report_interval_s"),
                    frame.get("rssi_floor"),
                    frame.get("channel"),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    sqlite_util.run_with_retry(_op, operation_name="upsert_anchor_status")


def _anchor_block(anchor_id: str, frame: dict[str, Any]) -> dict[str, Any]:
    """The `anchor` payload block the cloud worker uses to upsert
    ble_anchor_registry (last seen / link RSSI / firmware)."""
    return {
        "anchor_id": anchor_id,
        "gateway_id": frame.get("gateway_id"),
        "link_rssi": frame.get("link_rssi"),
        "fw": frame.get("fw"),
        "uptime_s": frame.get("uptime_s"),
        "free_heap": frame.get("free_heap"),
        "seq": frame.get("seq"),
    }


def build_scan_payload(ctx: IngestContext, anchor_id: str, frame: dict[str, Any]) -> dict[str, Any]:
    """Convert one anchor_scan frame into the ble_sensor_scan payload shape the
    Pi's own scans use (EDGE_DASHBOARD_CONTRACT.md), tagged with observer_id."""
    captured_at_utc = str(frame.get("bridge_received_at_utc") or _utc_now_iso())
    sensors: list[dict[str, Any]] = []
    for device in frame.get("devices") or []:
        if not isinstance(device, dict):
            continue
        mac = ctx.canonicalize_mac(str(device.get("mac", "")))
        if len(mac) != 17:
            continue
        sensors.append(
            {
                "device_id": mac,
                "mac_address": mac,
                "rssi": device.get("rssi"),
                "metadata": {
                    "max_rssi": device.get("max_rssi"),
                    "sample_count": device.get("count"),
                    "source": "ble_anchor",
                },
                "device_type": ctx.classify_mac(mac),
            }
        )

    window_s = frame.get("window_s")
    seq = frame.get("seq")
    part = frame.get("part", 0)
    return {
        "vehicle_id": ctx.vehicle_id,
        "pi_id": ctx.pi_id,
        "observer_id": anchor_id,
        "parent_pi_id": ctx.pi_id,
        "captured_at_utc": captured_at_utc,
        "event_type": "ble_sensor_scan",
        # seq/part come from the anchor and are unique per (anchor, boot);
        # captured_at disambiguates across reboots.
        "idempotency_key": (
            f"{ctx.vehicle_id}:ble_sensor_scan:{captured_at_utc}:{anchor_id}:{seq}:{part}"
        ),
        "scan_duration_seconds": window_s,
        "sensor_count": len(sensors),
        "sensors": sensors,
        "pi_location": ctx.get_pi_location(),
        "anchor": _anchor_block(anchor_id, frame),
    }


def build_heartbeat_payload(ctx: IngestContext, anchor_id: str, frame: dict[str, Any]) -> dict[str, Any]:
    """Anchor heartbeat as a zero-sensor ble_sensor_scan payload: keeps anchor
    liveness flowing to the cloud without a new event type."""
    captured_at_utc = str(frame.get("bridge_received_at_utc") or _utc_now_iso())
    return {
        "vehicle_id": ctx.vehicle_id,
        "pi_id": ctx.pi_id,
        "observer_id": anchor_id,
        "parent_pi_id": ctx.pi_id,
        "captured_at_utc": captured_at_utc,
        "event_type": "ble_sensor_scan",
        "idempotency_key": (
            f"{ctx.vehicle_id}:ble_sensor_scan:{captured_at_utc}:{anchor_id}:hb:{frame.get('seq')}"
        ),
        "scan_duration_seconds": None,
        "sensor_count": 0,
        "sensors": [],
        "pi_location": ctx.get_pi_location(),
        "anchor": _anchor_block(anchor_id, frame),
    }


def handle_frame(ctx: IngestContext, topic: str, frame: dict[str, Any]) -> None:
    """Process one MQTT message. Exceptions are caught by the caller."""
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != "anchors":
        return
    anchor_id, kind = parts[1], parts[2]
    if kind == "status" and anchor_id == "bridge":
        return  # the bridge's own liveness topic
    if not valid_anchor_id(anchor_id):
        return

    allowed = _anchor_allowed(ctx, anchor_id)

    if kind == "scan":
        _upsert_anchor_status(ctx, anchor_id, role="anchor", registered=allowed, frame=frame)
        if not allowed:
            _log_quarantined(ctx, anchor_id)
            return
        payload = build_scan_payload(ctx, anchor_id, frame)
        if payload["sensor_count"] > 0:
            ctx.record_observations(payload)
        ctx.enqueue_scan(payload)
    elif kind == "status":
        _upsert_anchor_status(ctx, anchor_id, role="anchor", registered=allowed, frame=frame)
        if not allowed:
            _log_quarantined(ctx, anchor_id)
            return
        ctx.enqueue_scan(build_heartbeat_payload(ctx, anchor_id, frame))
    elif kind == "gateway":
        # The gateway is an anchor too; its BLE scans arrive as anchor_scan
        # frames under its own id. This just tracks role + liveness.
        _upsert_anchor_status(ctx, anchor_id, role="gateway", registered=allowed, frame=frame)


# Bounded hand-off between the paho network thread and the SQLite writer
# thread. Sized for minutes of backlog at real anchor rates (7 anchors ×
# ~5 msgs/min); beyond that we shed load rather than grow without bound.
_WORK_QUEUE_MAX = 256


def start_ingest(ctx: IngestContext) -> Any:
    """Start the paho-mqtt network thread + a dedicated SQLite writer thread.
    Returns the client (or None when disabled/unavailable). Never raises:
    anchor ingest is an additive feature and must not take down the Pi's own
    BLE scanning.

    The paho callback only enqueues: SQLite writes can stall for the full
    busy-timeout (+retries) under lock contention, and blocking the network
    thread that long would starve MQTT keepalives and drop the connection.
    The writer thread absorbs the stall; a full queue sheds the newest frame
    (anchors re-report every window anyway)."""
    if not ctx.config.enabled:
        logger.info("Anchor ingest disabled (ANCHOR_INGEST_ENABLED=false).")
        return None
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logger.warning("paho-mqtt not installed; anchor ingest disabled.")
        return None

    work_queue: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue(maxsize=_WORK_QUEUE_MAX)

    def writer_loop() -> None:
        while True:
            topic, frame = work_queue.get()
            try:
                handle_frame(ctx, topic, frame)
            except Exception as exc:  # never kill the writer thread
                logger.error("Anchor ingest failed on %s: %s", topic, exc)
            finally:
                work_queue.task_done()

    threading.Thread(target=writer_loop, name="anchor-ingest-writer", daemon=True).start()

    def on_connect(client, _userdata, _flags, reason_code, _properties=None) -> None:
        logger.info("Anchor ingest connected to MQTT (%s)", reason_code)
        client.subscribe([("anchors/+/scan", 0), ("anchors/+/status", 0), ("anchors/+/gateway", 0)])

    def on_message(_client, _userdata, message) -> None:
        try:
            frame = json.loads(message.payload.decode("utf-8"))
            if not isinstance(frame, dict):
                return
            work_queue.put_nowait((message.topic, frame))
        except queue.Full:
            logger.warning(
                "Anchor ingest queue full (%d); dropping frame on %s.",
                _WORK_QUEUE_MAX,
                message.topic,
            )
        except Exception as exc:  # never kill the network thread
            logger.error("Anchor ingest failed to enqueue %s: %s", message.topic, exc)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ble-sensor-anchor-ingest")
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    # connect_async + loop_start: broker outages are retried in the paho
    # thread; ble-sensor's scan loop is never blocked.
    client.connect_async(ctx.config.mqtt_host, ctx.config.mqtt_port, keepalive=60)
    client.loop_start()
    logger.info(
        "Anchor ingest started (mqtt=%s:%d, registry=%s, allowlist=%d ids).",
        ctx.config.mqtt_host,
        ctx.config.mqtt_port,
        ctx.config.registry_path,
        len(ctx.config.allowlist),
    )
    return client
