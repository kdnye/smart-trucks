import asyncio
import json
import logging
import os
import shutil
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import uvloop

sys.path.append(str(Path(__file__).resolve().parents[1]))

from db import (
    get_db_stats,
    get_latest_power_snapshot,
    get_pending_gps_points,
    get_pending_heartbeats,
    increment_gps_attempts,
    increment_heartbeat_attempts,
    init_db,
    insert_gps_point,
    insert_heartbeat,
    mark_gps_points_sent,
    mark_heartbeats_sent,
    purge_old_sent_rows,
    record_edge_health,
)
from imu_reader import IMUReader, ImuSnapshot, snapshot_as_dict
from nmea_reader import GpsReading, NMEAReader
from shared.hardware_probe import (
    build_hardware_inventory,
    parse_bool_env,
    parse_hex_list_env,
    parse_int_env,
    validate_inventory,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    vehicle_id: str
    webhook_url: str | None
    api_key: str
    gps_sample_interval_seconds: int
    heartbeat_interval_seconds: int
    sync_interval_seconds: int
    gps_serial_candidates: tuple[str, ...]
    gps_probe_all_candidates: bool
    gps_baud_rate: int
    sync_batch_size: int
    sync_backoff_max_seconds: int
    queue_alert_depth: int
    power_snapshot_max_age_seconds: int
    imu_i2c_bus: int
    imu_expected_addresses: tuple[int, ...]
    imu_required: bool


def _sanitize_env_value(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None

    value = raw_value.strip()
    if not value:
        return None

    if value.startswith("${") and value.endswith("}"):
        body = value[2:-1]
        if ":-" in body:
            _, fallback = body.split(":-", 1)
            fallback_value = fallback.strip()
            return fallback_value or None
        return None

    return value


def _read_str_env(name: str, default: str | None = None) -> str | None:
    value = _sanitize_env_value(os.getenv(name))
    if value is not None:
        return value
    return default


def load_config() -> Config:
    raw_candidates = _read_str_env("GPS_SERIAL_CANDIDATES", "/dev/serial0,/dev/ttyS0") or ""
    serial_candidates = tuple(
        candidate.strip()
        for candidate in raw_candidates.split(",")
        if candidate.strip()
    )
    primary_device = _read_str_env("GPS_SERIAL_DEVICE", "/dev/serial0") or "/dev/serial0"
    probe_all_candidates = (_read_str_env("GPS_PROBE_ALL_CANDIDATES", "false") or "false").lower() in {
        "1",
        "true",
        "yes",
    }
    serial_devices: tuple[str, ...]
    if probe_all_candidates:
        serial_devices = tuple(dict.fromkeys((primary_device, *serial_candidates)))
    else:
        serial_devices = (primary_device,)
    return Config(
        vehicle_id=_read_str_env("VEHICLE_ID", "UNKNOWN_TRUCK") or "UNKNOWN_TRUCK",
        webhook_url=_read_str_env("WEBHOOK_URL"),
        api_key=_read_str_env("API_KEY", "") or "",
        gps_sample_interval_seconds=max(1, int(_read_str_env("GPS_SAMPLE_INTERVAL_SECONDS", "5") or "5")),
        heartbeat_interval_seconds=max(10, int(_read_str_env("HEARTBEAT_INTERVAL_SECONDS", "60") or "60")),
        sync_interval_seconds=max(5, int(_read_str_env("SYNC_INTERVAL_SECONDS", "20") or "20")),
        gps_serial_candidates=serial_devices,
        gps_probe_all_candidates=probe_all_candidates,
        gps_baud_rate=max(1200, int(_read_str_env("GPS_BAUD_RATE", "9600") or "9600")),
        sync_batch_size=max(10, int(_read_str_env("SYNC_BATCH_SIZE", "50") or "50")),
        sync_backoff_max_seconds=max(30, int(_read_str_env("SYNC_BACKOFF_MAX_SECONDS", "300") or "300")),
        queue_alert_depth=max(100, int(_read_str_env("QUEUE_ALERT_DEPTH", "1000") or "1000")),
        power_snapshot_max_age_seconds=max(5, int(_read_str_env("POWER_SNAPSHOT_MAX_AGE_SECONDS", "30") or "30")),
        imu_i2c_bus=parse_int_env("IMU_I2C_BUS", 1, minimum=0),
        imu_expected_addresses=parse_hex_list_env("IMU_EXPECTED_ADDRESSES", (0x6A,)),
        imu_required=parse_bool_env("IMU_REQUIRED", True),
    )


@dataclass
class RuntimeState:
    start_monotonic: float
    local_sequence: int = 0
    last_gps_fix_utc: str | None = None
    last_locked_gps_point_utc: str | None = None
    last_upload_success_utc: str | None = None
    gps_reader_ok: bool = True
    uploader_ok: bool = True
    power_monitor_ok: bool = True
    wifi_connected: bool = False
    latest_valid_gps: dict[str, Any] | None = None


class ImuMonitor:
    def __init__(self, bus_num: int) -> None:
        self._reader = IMUReader(bus_num=bus_num)
        self._latest_snapshot: dict[str, Any] = {"status": "initializing"}
        self._latest_harsh_event: dict[str, Any] | None = None

    async def start(self) -> None:
        async def on_snapshot(snapshot: ImuSnapshot) -> None:
            self._latest_snapshot = {"status": "ok", **snapshot_as_dict(snapshot)}

        async def on_harsh_event(snapshot: ImuSnapshot) -> None:
            self._latest_harsh_event = snapshot_as_dict(snapshot)

        await self._reader.read_loop(on_snapshot, on_harsh_event)

    def read(self) -> dict[str, Any]:
        payload = dict(self._latest_snapshot)
        if self._latest_harsh_event:
            payload["latest_harsh_event"] = self._latest_harsh_event
        return payload


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def age_seconds(timestamp: str | None) -> int | None:
    parsed = parse_iso_utc(timestamp)
    if not parsed:
        return None
    return int((datetime.now(timezone.utc) - parsed).total_seconds())


def is_network_connected() -> bool:
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=1.5):
            return True
    except OSError:
        return False


def get_latest_gps(state: RuntimeState) -> dict[str, Any]:
    if state.latest_valid_gps:
        return dict(state.latest_valid_gps)
    return {"fix_status": "searching"}


def build_location_payload(gps: dict[str, Any]) -> dict[str, Any]:
    """Build canonical + compatibility GPS payload fields for downstream consumers."""
    location = dict(gps)
    fix_status = location.get("fix_status", "searching")

    if fix_status != "locked":
        return {"fix_status": "searching"}

    latitude = location.get("latitude")
    longitude = location.get("longitude")

    return {
        **location,
        # Redundant aliases for downstream parsers that expect different keys.
        "lat": latitude,
        "lng": longitude,
        "lon": longitude,
        # Compatibility for older dashboards that still read speed_knots.
        "speed_knots": (location.get("speed_kmh") / 1.852) if location.get("speed_kmh") is not None else None,
    }


def _is_valid_fix(reading: GpsReading) -> bool:
    if reading.latitude is None or reading.longitude is None:
        return False
    return reading.fix_quality > 0 or reading.fix_type >= 2


async def gps_reader_worker(config: Config, state: RuntimeState) -> None:
    candidates = list(config.gps_serial_candidates)
    if not candidates:
        print("GPS reader disabled: no serial candidates configured.")
        return

    selected_device = candidates[0]
    tcp_mode = selected_device.startswith("tcp://")
    if config.gps_probe_all_candidates and not tcp_mode:
        for candidate in candidates:
            if os.path.exists(candidate):
                selected_device = candidate
                break
    elif not tcp_mode and not os.path.exists(selected_device):
        print(
            "Primary GPS serial path unavailable. "
            f"device={selected_device} errno=2 exception_type=FileNotFoundError"
        )

    print(
        "Preparing GPS reader. "
        f"device={selected_device} tcp_mode={selected_device.startswith('tcp://')} "
        f"probe_all_candidates={config.gps_probe_all_candidates}"
    )
    reader = NMEAReader(port=selected_device, baudrate=config.gps_baud_rate)
    print(
        "Starting GPS reader task. "
        f"device={selected_device} baud={config.gps_baud_rate} probe_all_candidates={config.gps_probe_all_candidates}"
    )

    async def on_reading(reading: GpsReading) -> None:
        if not _is_valid_fix(reading):
            return

        captured_at = utc_now_iso()
        state.last_gps_fix_utc = captured_at
        state.gps_reader_ok = True
        state.latest_valid_gps = {
            "fix_status": "locked",
            "device": selected_device,
            "latitude": reading.latitude,
            "longitude": reading.longitude,
            "altitude_m": reading.altitude,
            "speed_kmh": reading.speed_kmh,
            "gps_timestamp": reading.timestamp.isoformat() if reading.timestamp else None,
        }

    retry_backoff_seconds = 1.0
    max_retry_backoff_seconds = 30.0

    while True:
        try:
            await reader.read_loop(on_reading)
            state.gps_reader_ok = False
            print("GPS reader loop exited unexpectedly. Restarting reader.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-except
            state.gps_reader_ok = False
            print(f"GPS reader worker crashed: {exc}. Restarting in {retry_backoff_seconds:.1f}s.")

        await asyncio.sleep(retry_backoff_seconds)
        retry_backoff_seconds = min(max_retry_backoff_seconds, retry_backoff_seconds * 2)


async def post_payload(
    session: aiohttp.ClientSession,
    config: Config,
    payload: dict[str, Any],
    idempotency_key: str,
) -> bool:
    if not config.webhook_url:
        return False

    headers = {"X-Idempotency-Key": idempotency_key}
    if config.api_key:
        headers["X-Api-Key"] = config.api_key

    try:
        async with session.post(config.webhook_url, json=payload, headers=headers) as response:
            if 200 <= response.status < 300:
                return True
            body = await response.text()
            print(f"Sync failed. Status={response.status} Body={body[:200]}")
            return False
    except Exception as exc:
        print(f"Cloud connection failed: {exc}")
        return False


async def gps_collector_worker(config: Config, state: RuntimeState) -> None:
    while True:
        captured_at = utc_now_iso()
        gps = build_location_payload(get_latest_gps(state))
        state.local_sequence += 1

        if gps.get("fix_status") == "locked":
            state.last_locked_gps_point_utc = captured_at
        else:
            stale_for = age_seconds(state.last_gps_fix_utc)
            state.gps_reader_ok = stale_for is None or stale_for <= 300

        payload = {
            "event_type": "gps_point",
            "pi_device_id": os.environ.get("BALENA_DEVICE_NAME_AT_INIT", "Unknown_Pi"),
            "motive_vehicle_id": os.environ.get("MOTIVE_VEHICLE_ID"),
            "captured_at_utc": captured_at,
            "local_sequence": state.local_sequence,
            "location": gps,
        }

        await insert_gps_point(
            vehicle_id=config.vehicle_id,
            captured_at_utc=captured_at,
            lat=gps.get("latitude"),
            lon=gps.get("longitude"),
            speed_kmh=gps.get("speed_kmh"),
            fix_status=gps.get("fix_status", "searching"),
            source_device=gps.get("device"),
            trip_id=None,
            local_sequence=state.local_sequence,
            payload=payload,
        )

        await asyncio.sleep(config.gps_sample_interval_seconds)


async def heartbeat_builder_worker(config: Config, state: RuntimeState, imu: ImuMonitor) -> None:
    while True:
        captured_at = utc_now_iso()
        gps_payload = build_location_payload(get_latest_gps(state))
        state.wifi_connected = is_network_connected()
        db_stats = await get_db_stats()
        latest_power_snapshot = await get_latest_power_snapshot(config.vehicle_id)
        power_payload = latest_power_snapshot.get("payload", {}) if latest_power_snapshot else {}
        snapshot_timestamp = latest_power_snapshot.get("occurred_at") if latest_power_snapshot else None
        snapshot_age_sec = age_seconds(snapshot_timestamp)
        snapshot_found = latest_power_snapshot is not None
        snapshot_stale = (
            snapshot_age_sec is None
            or snapshot_age_sec > config.power_snapshot_max_age_seconds
        )
        power_metrics = {
            **power_payload,
            "source": "power_snapshot_db",
            "snapshot_found": snapshot_found,
            "snapshot_stale": snapshot_stale,
            "snapshot_age_sec": snapshot_age_sec,
            "snapshot_captured_at_utc": snapshot_timestamp,
        }
        if not snapshot_found:
            power_metrics["status"] = "absent"

        state.power_monitor_ok = (
            snapshot_found
            and not snapshot_stale
            and power_payload.get("status") == "ok"
        )

        heartbeat_payload = {
            "event_type": "edge_telematics_heartbeat",
            "vehicle_id": config.vehicle_id,
            "captured_at_utc": captured_at,
            "process_uptime_sec": int(time.monotonic() - state.start_monotonic),
            # Keep GPS in multiple locations to maximize compatibility while
            # clients migrate to location/location_status.
            "location": gps_payload,
            "gps": gps_payload,
            "location_status": {
                "fix_status": "locked" if state.last_gps_fix_utc else "searching",
                "last_fix_age_sec": age_seconds(state.last_gps_fix_utc),
            },
            "power_metrics": power_metrics,
            "imu_metrics": imu.read(),
            "queue": db_stats,
            "wifi_connected": state.wifi_connected,
        }
        await insert_heartbeat(
            vehicle_id=config.vehicle_id,
            heartbeat_type="heartbeat",
            captured_at_utc=captured_at,
            payload=heartbeat_payload,
        )

        await asyncio.sleep(config.heartbeat_interval_seconds)


async def sync_worker(config: Config, state: RuntimeState, session: aiohttp.ClientSession) -> None:
    backoff_seconds = config.sync_interval_seconds

    while True:
        if not config.webhook_url:
            await asyncio.sleep(config.sync_interval_seconds)
            continue

        if not is_network_connected():
            state.wifi_connected = False
            state.uploader_ok = False
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(config.sync_backoff_max_seconds, backoff_seconds * 2)
            continue

        state.wifi_connected = True
        did_send = False

        pending_gps = await get_pending_gps_points(limit=config.sync_batch_size)
        for row_id, captured_at, payload_json, local_sequence in pending_gps:
            payload = json.loads(payload_json)
            idempotency_key = f"{config.vehicle_id}:{captured_at}:{local_sequence}"
            await increment_gps_attempts([row_id])
            success = await post_payload(session, config, payload, idempotency_key)
            if success:
                await mark_gps_points_sent([row_id])
                state.last_upload_success_utc = utc_now_iso()
                state.uploader_ok = True
                did_send = True
            else:
                state.uploader_ok = False
                break

        if state.uploader_ok:
            pending_heartbeats = await get_pending_heartbeats(limit=config.sync_batch_size)
            for row_id, captured_at, payload_json in pending_heartbeats:
                payload = json.loads(payload_json)
                idempotency_key = f"{config.vehicle_id}:{captured_at}:heartbeat:{row_id}"
                await increment_heartbeat_attempts([row_id])
                success = await post_payload(session, config, payload, idempotency_key)
                if success:
                    await mark_heartbeats_sent([row_id])
                    state.last_upload_success_utc = utc_now_iso()
                    state.uploader_ok = True
                    did_send = True
                else:
                    state.uploader_ok = False
                    break

        if did_send:
            backoff_seconds = config.sync_interval_seconds
        else:
            backoff_seconds = min(config.sync_backoff_max_seconds, max(config.sync_interval_seconds, backoff_seconds))

        await asyncio.sleep(backoff_seconds)


async def maintenance_worker(config: Config, state: RuntimeState) -> None:
    while True:
        await asyncio.sleep(60)

        db_stats = await get_db_stats()
        disk = shutil.disk_usage("/")
        disk_free_mb = round(disk.free / (1024 * 1024), 2)

        last_gps_fix_age = age_seconds(state.last_gps_fix_utc)
        last_upload_age = age_seconds(state.last_upload_success_utc)
        queue_depth = db_stats["queue_depth"]

        alerts: list[str] = []
        if last_gps_fix_age is not None and last_gps_fix_age > 300:
            alerts.append("gps_reader_stale")
            state.gps_reader_ok = False

        if last_upload_age is not None and last_upload_age > 300 and queue_depth > 0:
            alerts.append("uploader_stale")
            state.uploader_ok = False

        if queue_depth > config.queue_alert_depth:
            alerts.append("queue_depth_high")

        if disk_free_mb < 250:
            alerts.append("disk_free_low")

        state.wifi_connected = is_network_connected()
        if not state.wifi_connected:
            alerts.append("wifi_disconnected")

        edge_payload = {
            "event_type": "edge_health",
            "vehicle_id": config.vehicle_id,
            "captured_at_utc": utc_now_iso(),
            "last_gps_fix_utc": state.last_gps_fix_utc,
            "last_upload_success_utc": state.last_upload_success_utc,
            "last_gps_fix_age_sec": last_gps_fix_age,
            "last_locked_gps_point_age_sec": age_seconds(state.last_locked_gps_point_utc),
            "last_upload_success_age_sec": last_upload_age,
            "pending_gps_points": db_stats["pending_gps_points"],
            "pending_heartbeats": db_stats["pending_heartbeats"],
            "wifi_connected": state.wifi_connected,
            "gps_reader_ok": state.gps_reader_ok,
            "uploader_ok": state.uploader_ok,
            "power_monitor_ok": state.power_monitor_ok,
            "disk_free_mb": disk_free_mb,
            "process_uptime_sec": int(time.monotonic() - state.start_monotonic),
            "alerts": alerts,
        }

        await insert_heartbeat(
            vehicle_id=config.vehicle_id,
            heartbeat_type="edge_health",
            captured_at_utc=edge_payload["captured_at_utc"],
            payload=edge_payload,
        )
        await record_edge_health(
            {
                "captured_at_utc": edge_payload["captured_at_utc"],
                "last_gps_fix_utc": state.last_gps_fix_utc,
                "last_upload_success_utc": state.last_upload_success_utc,
                "queue_depth": queue_depth,
                "disk_free_mb": disk_free_mb,
                "wifi_state": "connected" if state.wifi_connected else "disconnected",
                "process_state": "degraded" if alerts else "ok",
                "alerts": alerts,
            }
        )

        if disk_free_mb < 250:
            await purge_old_sent_rows(days=3)
        else:
            await purge_old_sent_rows(days=7)


async def run() -> None:
    if os.getenv("DEVICE_ROLE", "truck").lower() == "warehouse":
        logger.info("DEVICE_ROLE is set to warehouse. Disabling GPS and IMU hardware probes.")
        logger.info("Telematics Edge container going into idle sleep mode.")
        while True:
            await asyncio.sleep(86400)

    config = load_config()
    # TCP addresses (e.g. tcp://gps-multiplexer:2947) are served by the
    # gps-multiplexer container and must not be probed as serial ports.
    serial_probe_candidates = tuple(
        c for c in config.gps_serial_candidates if not c.startswith("tcp://")
    )
    inventory = build_hardware_inventory(
        gps_candidates=serial_probe_candidates,
        gps_baud_rate=config.gps_baud_rate,
        i2c_bus=config.imu_i2c_bus,
        ups_expected_addresses=tuple(),
        imu_expected_addresses=config.imu_expected_addresses,
        probe_serial=True,
    )
    print(f"Hardware inventory: {inventory.to_json()}")
    os.makedirs("/data", exist_ok=True)
    with open("/data/telematics_hardware_inventory.json", "w", encoding="utf-8") as handle:
        json.dump(inventory.to_dict(), handle, sort_keys=True)
    inventory_errors = validate_inventory(inventory, imu_required=config.imu_required, ups_required=False)
    if inventory_errors:
        raise RuntimeError(f"Hardware probe failed: {'; '.join(inventory_errors)}")

    imu = ImuMonitor(bus_num=config.imu_i2c_bus)
    state = RuntimeState(start_monotonic=time.monotonic())

    await init_db()
    print(f"Starting telematics-edge for vehicle {config.vehicle_id}.")

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [
            asyncio.create_task(imu.start()),
            asyncio.create_task(gps_reader_worker(config, state)),
            asyncio.create_task(gps_collector_worker(config, state)),
            asyncio.create_task(heartbeat_builder_worker(config, state, imu)),
            asyncio.create_task(sync_worker(config, state, session)),
            asyncio.create_task(maintenance_worker(config, state)),
        ]
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    uvloop.install()
    asyncio.run(run())
