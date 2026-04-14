import asyncio
import json
import os
import shutil
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
import pynmea2
import serial
from ina219 import INA219, DeviceRangeError

from db import (
    get_db_stats,
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


@dataclass(frozen=True)
class Config:
    vehicle_id: str
    webhook_url: str | None
    api_key: str
    gps_sample_interval_seconds: int
    heartbeat_interval_seconds: int
    sync_interval_seconds: int
    gps_serial_candidates: tuple[str, ...]
    gps_baud_rate: int
    sync_batch_size: int
    sync_backoff_max_seconds: int
    queue_alert_depth: int


def load_config() -> Config:
    # Default to /dev/serial0 only — the stable alias on Raspberry Pi.
    # /dev/ttyAMA0 does not exist on Pi Zero W2 and causes Errno 2 errors.
    serial_candidates = tuple(
        candidate.strip()
        for candidate in os.getenv("GPS_SERIAL_CANDIDATES", "/dev/serial0").split(",")
        if candidate.strip()
    )
    primary_device = os.getenv("GPS_SERIAL_DEVICE", "/dev/serial0")
    serial_devices: tuple[str, ...] = tuple(dict.fromkeys((primary_device, *serial_candidates)))
    return Config(
        vehicle_id=os.getenv("VEHICLE_ID", "UNKNOWN_TRUCK"),
        webhook_url=os.getenv("WEBHOOK_URL"),
        api_key=os.getenv("API_KEY", ""),
        gps_sample_interval_seconds=max(1, int(os.getenv("GPS_SAMPLE_INTERVAL_SECONDS", "5"))),
        heartbeat_interval_seconds=max(10, int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "60"))),
        sync_interval_seconds=max(5, int(os.getenv("SYNC_INTERVAL_SECONDS", "20"))),
        gps_serial_candidates=serial_devices,
        gps_baud_rate=max(1200, int(os.getenv("GPS_BAUD_RATE", "9600"))),
        sync_batch_size=max(10, int(os.getenv("SYNC_BATCH_SIZE", "50"))),
        sync_backoff_max_seconds=max(30, int(os.getenv("SYNC_BACKOFF_MAX_SECONDS", "300"))),
        queue_alert_depth=max(100, int(os.getenv("QUEUE_ALERT_DEPTH", "1000"))),
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


class PowerMonitor:
    def __init__(self, i2c_address: int = 0x43, shunt_ohms: float = 0.1) -> None:
        self._active = False
        self._ina: INA219 | None = None
        try:
            self._ina = INA219(shunt_ohms=shunt_ohms, address=i2c_address)
            self._ina.configure()
            self._active = True
            print(f"UPS HAT battery monitor initialized at I2C 0x{i2c_address:02X}.")
        except Exception as exc:
            print(f"Warning: Could not initialize UPS HAT monitor on 0x{i2c_address:02X}: {exc}")

    def read(self) -> dict[str, Any]:
        if not self._active or not self._ina:
            return {"status": "offline"}

        try:
            voltage = self._ina.voltage()
            current = self._ina.current()
            power = self._ina.power()
            return {
                "status": "ok",
                "voltage_v": round(voltage, 3),
                "current_ma": round(current, 3),
                "power_mw": round(power, 3),
                "is_charging": current > 0,
            }
        except DeviceRangeError:
            return {"status": "overflow_error"}
        except Exception as exc:
            return {"status": "read_error", "message": str(exc)}


class ImuMonitor:
    def __init__(self) -> None:
        self._reader = IMUReader()
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


def get_latest_gps(serial_devices: tuple[str, ...], baud_rate: int) -> dict[str, Any]:
    for serial_device in serial_devices:
        # Skip paths that don't exist in the filesystem to avoid noisy Errno 2 errors.
        if not os.path.exists(serial_device):
            print(f"GPS device not present, skipping: {serial_device}")
            continue

        try:
            with serial.Serial(serial_device, baud_rate, timeout=2.0) as ser:
                for _ in range(20):
                    line = ser.readline().decode("ascii", errors="replace").strip()
                    if not line.startswith(("$GPRMC", "$GPGGA", "$GNRMC", "$GNGGA")):
                        continue

                    try:
                        msg = pynmea2.parse(line)
                    except pynmea2.ParseError:
                        continue

                    latitude = getattr(msg, "latitude", 0.0)
                    longitude = getattr(msg, "longitude", 0.0)
                    if not latitude and not longitude:
                        continue

                    speed_knots = getattr(msg, "spd_over_grnd", None)
                    speed_kmh = float(speed_knots) * 1.852 if speed_knots else None
                    return {
                        "fix_status": "locked",
                        "device": serial_device,
                        "latitude": latitude,
                        "longitude": longitude,
                        "altitude_m": getattr(msg, "altitude", None),
                        "speed_kmh": speed_kmh,
                        "gps_timestamp": str(getattr(msg, "timestamp", "")) or None,
                    }

            # Port opened successfully but yielded no parseable fix sentences.
            print(f"GPS device {serial_device} ready but returned no valid fix data in 20 lines")

        except Exception as exc:
            print(f"GPS serial error on {serial_device}: {exc}")

    return {"fix_status": "searching"}


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
        gps = get_latest_gps(config.gps_serial_candidates, config.gps_baud_rate)
        state.local_sequence += 1

        if gps.get("fix_status") == "locked":
            state.last_gps_fix_utc = captured_at
            state.last_locked_gps_point_utc = captured_at
            state.gps_reader_ok = True
        else:
            stale_for = age_seconds(state.last_gps_fix_utc)
            state.gps_reader_ok = stale_for is None or stale_for <= 300

        payload = {
            "event_type": "gps_point",
            "vehicle_id": config.vehicle_id,
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


async def heartbeat_builder_worker(config: Config, state: RuntimeState, power: PowerMonitor, imu: ImuMonitor) -> None:
    while True:
        captured_at = utc_now_iso()
        state.wifi_connected = is_network_connected()
        db_stats = await get_db_stats()
        power_metrics = power.read()
        state.power_monitor_ok = power_metrics.get("status") == "ok"

        heartbeat_payload = {
            "event_type": "edge_telematics_heartbeat",
            "vehicle_id": config.vehicle_id,
            "captured_at_utc": captured_at,
            "process_uptime_sec": int(time.monotonic() - state.start_monotonic),
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
    config = load_config()
    power = PowerMonitor()
    imu = ImuMonitor()
    state = RuntimeState(start_monotonic=time.monotonic())

    await init_db()
    print(f"Starting telematics-edge for vehicle {config.vehicle_id}.")

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = [
            asyncio.create_task(imu.start()),
            asyncio.create_task(gps_collector_worker(config, state)),
            asyncio.create_task(heartbeat_builder_worker(config, state, power, imu)),
            asyncio.create_task(sync_worker(config, state, session)),
            asyncio.create_task(maintenance_worker(config, state)),
        ]
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(run())
