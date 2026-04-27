import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiosqlite
import uvloop

sys.path.append(str(Path(__file__).resolve().parents[1]))

from db import (
    get_db_stats,
    get_latest_power_snapshot,
    init_db,
    insert_heartbeat,
    pop_beacon_wake_signal,
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
    device_role: str
    gps_sample_interval_seconds: int
    heartbeat_interval_seconds: int
    gps_serial_candidates: tuple[str, ...]
    gps_probe_all_candidates: bool
    gps_baud_rate: int
    queue_alert_depth: int
    power_snapshot_max_age_seconds: int
    imu_i2c_bus: int
    imu_expected_addresses: tuple[int, ...]
    imu_required: bool
    warehouse_latitude: float | None = None
    warehouse_longitude: float | None = None
    network_watchdog_enabled: bool = True
    network_watchdog_check_interval_seconds: int = 60
    network_watchdog_max_failures: int = 3
    network_watchdog_recovery_pause_seconds: int = 30
    network_watchdog_connection_name: str | None = None


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


def _read_float_env(name: str) -> float | None:
    value = _read_str_env(name)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        raise ValueError(
            f"{name} must be a numeric value; received {value!r}. "
            f"Action: set {name} to a valid decimal number in Balena device/fleet variables."
        ) from None


def load_config() -> Config:
    device_role = (_read_str_env("DEVICE_ROLE", "truck") or "truck").lower()
    warehouse_latitude = _read_float_env("WAREHOUSE_LAT")
    warehouse_longitude = _read_float_env("WAREHOUSE_LON")
    if device_role == "warehouse" and (warehouse_latitude is None or warehouse_longitude is None):
        raise RuntimeError(
            "DEVICE_ROLE=warehouse requires WAREHOUSE_LAT and WAREHOUSE_LON. "
            "Action: set both variables to valid decimal coordinates in Balena for this device."
        )

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
        device_role=device_role,
        gps_sample_interval_seconds=max(1, int(_read_str_env("GPS_SAMPLE_INTERVAL_SECONDS", "5") or "5")),
        heartbeat_interval_seconds=max(10, int(_read_str_env("HEARTBEAT_INTERVAL_SECONDS", "60") or "60")),
        gps_serial_candidates=serial_devices,
        gps_probe_all_candidates=probe_all_candidates,
        gps_baud_rate=max(1200, int(_read_str_env("GPS_BAUD_RATE", "9600") or "9600")),
        queue_alert_depth=max(100, int(_read_str_env("QUEUE_ALERT_DEPTH", "1000") or "1000")),
        power_snapshot_max_age_seconds=max(5, int(_read_str_env("POWER_SNAPSHOT_MAX_AGE_SECONDS", "30") or "30")),
        network_watchdog_enabled=(_read_str_env("NETWORK_WATCHDOG_ENABLED", "true") or "true").lower() in {"1", "true", "yes"},
        network_watchdog_check_interval_seconds=max(
            15, int(_read_str_env("NETWORK_WATCHDOG_CHECK_INTERVAL_SECONDS", "60") or "60")
        ),
        network_watchdog_max_failures=max(1, int(_read_str_env("NETWORK_WATCHDOG_MAX_FAILURES", "3") or "3")),
        network_watchdog_recovery_pause_seconds=max(
            5, int(_read_str_env("NETWORK_WATCHDOG_RECOVERY_PAUSE_SECONDS", "30") or "30")
        ),
        network_watchdog_connection_name=_read_str_env("NETWORK_WATCHDOG_CONNECTION_NAME"),
        imu_i2c_bus=parse_int_env("IMU_I2C_BUS", 1, minimum=0),
        imu_expected_addresses=parse_hex_list_env("IMU_EXPECTED_ADDRESSES", (0x6A,)),
        imu_required=parse_bool_env("IMU_REQUIRED", True),
        warehouse_latitude=warehouse_latitude,
        warehouse_longitude=warehouse_longitude,
    )


@dataclass
class RuntimeState:
    start_monotonic: float
    local_sequence: int = 0
    last_gps_fix_utc: str | None = None
    last_locked_gps_point_utc: str | None = None
    gps_reader_ok: bool = True
    power_monitor_ok: bool = True
    wifi_connected: bool = False
    latest_valid_gps: dict[str, Any] | None = None
    parked_mode: bool = False
    park_wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    last_motion_monotonic: float = 0.0


class ImuMonitor:
    def __init__(self, bus_num: int) -> None:
        self._reader = IMUReader(bus_num=bus_num)
        self._latest_snapshot: dict[str, Any] = {"status": "initializing"}
        self._latest_harsh_event: dict[str, Any] | None = None
        self._motion_wake_callback: Callable[[], Awaitable[None]] | None = None

    def set_motion_wake_callback(self, cb: Callable[[], Awaitable[None]]) -> None:
        self._motion_wake_callback = cb

    async def start(self) -> None:
        async def on_snapshot(snapshot: ImuSnapshot) -> None:
            self._latest_snapshot = {"status": "ok", **snapshot_as_dict(snapshot)}

        async def on_harsh_event(snapshot: ImuSnapshot) -> None:
            self._latest_harsh_event = snapshot_as_dict(snapshot)
            if self._motion_wake_callback:
                await self._motion_wake_callback()

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


def _run_network_recovery_command(command: list[str]) -> bool:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=20)
        if result.returncode == 0:
            return True
        logger.warning(
            "Network watchdog command failed: cmd=%s returncode=%s stderr=%s",
            command,
            result.returncode,
            (result.stderr or "").strip(),
        )
        return False
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Network watchdog command error: cmd=%s error=%s", command, exc)
        return False


async def network_watchdog_worker(config: Config, state: RuntimeState) -> None:
    if not config.network_watchdog_enabled:
        logger.info("Network watchdog disabled by configuration.")
        return
    if not shutil.which("nmcli"):
        logger.warning("Network watchdog enabled but nmcli is unavailable in container; skipping recovery actions.")
        return

    consecutive_failures = 0
    logger.info(
        "Network watchdog enabled: check_interval=%ss max_failures=%s",
        config.network_watchdog_check_interval_seconds,
        config.network_watchdog_max_failures,
    )
    while True:
        if is_network_connected():
            if consecutive_failures > 0:
                logger.info("Network watchdog: connectivity restored after %s failed checks.", consecutive_failures)
            if state.parked_mode:
                logger.info("Network watchdog: WiFi restored — exiting parked mode.")
                state.parked_mode = False
                state.park_wake_event.set()
            consecutive_failures = 0
            state.wifi_connected = True
            await asyncio.sleep(config.network_watchdog_check_interval_seconds)
            continue

        consecutive_failures += 1
        state.wifi_connected = False
        logger.warning(
            "Network watchdog check failed (%s/%s).",
            consecutive_failures,
            config.network_watchdog_max_failures,
        )
        if consecutive_failures < config.network_watchdog_max_failures:
            await asyncio.sleep(config.network_watchdog_check_interval_seconds)
            continue

        # Already in parked mode — skip the radio-cycle recovery and let parked_scan_worker
        # handle periodic WiFi re-checks.
        if state.parked_mode:
            consecutive_failures = 0
            await asyncio.sleep(config.network_watchdog_check_interval_seconds)
            continue

        logger.error("Network watchdog triggering Wi-Fi recovery sequence via NetworkManager.")
        _run_network_recovery_command(["nmcli", "device", "wifi", "rescan"])
        await asyncio.sleep(5)
        _run_network_recovery_command(["nmcli", "radio", "wifi", "off"])
        await asyncio.sleep(2)
        _run_network_recovery_command(["nmcli", "radio", "wifi", "on"])
        if config.network_watchdog_connection_name:
            _run_network_recovery_command(["nmcli", "connection", "up", config.network_watchdog_connection_name])

        consecutive_failures = 0
        if not is_network_connected() and not state.parked_mode:
            if _truck_is_active(state):
                logger.info(
                    "Network watchdog: WiFi recovery failed but truck is active — "
                    "staying in active mode, data will queue until WiFi returns."
                )
            else:
                logger.warning("Network watchdog: WiFi recovery failed and truck inactive — entering parked mode.")
                state.parked_mode = True
        await asyncio.sleep(config.network_watchdog_recovery_pause_seconds)


def get_latest_gps(config: Config, state: RuntimeState) -> dict[str, Any]:
    if config.device_role == "warehouse":
        return {
            "fix_status": "locked",
            "device": "warehouse_config",
            "latitude": config.warehouse_latitude,
            "longitude": config.warehouse_longitude,
            "altitude_m": None,
            "speed_kmh": 0.0,
            "gps_timestamp": None,
        }
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


def build_power_metrics_payload(
    latest_power_snapshot: dict[str, Any] | None,
    *,
    max_snapshot_age_seconds: int,
) -> tuple[dict[str, Any], bool]:
    """Build canonical + compatibility power metrics fields for downstream consumers."""
    power_payload = latest_power_snapshot.get("payload", {}) if latest_power_snapshot else {}
    snapshot_timestamp = latest_power_snapshot.get("occurred_at") if latest_power_snapshot else None
    snapshot_age_sec = age_seconds(snapshot_timestamp)
    snapshot_found = latest_power_snapshot is not None
    snapshot_stale = (
        snapshot_age_sec is None
        or snapshot_age_sec > max_snapshot_age_seconds
    )

    # Canonical contract uses `voltage_v`; power-monitor currently persists `bus_voltage_v`.
    # Keep both fields so existing dashboards continue to work during migration.
    voltage_v = power_payload.get("voltage_v")
    if voltage_v is None:
        voltage_v = power_payload.get("bus_voltage_v")

    power_metrics = {
        **power_payload,
        "voltage_v": voltage_v,
        "source": "power_snapshot_db",
        "snapshot_found": snapshot_found,
        "snapshot_stale": snapshot_stale,
        "snapshot_age_sec": snapshot_age_sec,
        "snapshot_captured_at_utc": snapshot_timestamp,
    }
    if not snapshot_found:
        power_metrics["status"] = "absent"

    power_monitor_ok = (
        snapshot_found
        and not snapshot_stale
        and power_payload.get("status") == "ok"
    )
    return power_metrics, power_monitor_ok


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



PARKED_SLEEP_SECONDS = 50.0
# Truck is considered active for this many seconds after the last IMU motion event.
ACTIVE_AFTER_MOTION_SECONDS = 300.0
# GPS speed threshold below which the truck is considered stationary.
_MOVING_SPEED_KMH = 5.0
LOCAL_DB_PATH = "/data/telematics.db"
SQLITE_BUSY_TIMEOUT_MS = int(_read_str_env("SQLITE_BUSY_TIMEOUT_MS", "30000") or "30000")


def _adaptive_parked_sleep_seconds(power: dict[str, Any] | None) -> float:
    """Return parked sleep duration scaled by battery SOC to preserve charge."""
    if not power or bool(power.get("is_charging")):
        return PARKED_SLEEP_SECONDS
    soc = float(power.get("state_of_charge_pct_estimate", 100.0))
    if soc < 10.0:
        return 300.0
    if soc < 25.0:
        return 120.0
    return PARKED_SLEEP_SECONDS


def _truck_is_active(state: RuntimeState) -> bool:
    """Return True if recent motion or GPS speed suggests the truck is in use."""
    if state.last_motion_monotonic > 0:
        if time.monotonic() - state.last_motion_monotonic < ACTIVE_AFTER_MOTION_SECONDS:
            return True
    gps = state.latest_valid_gps
    if gps and (gps.get("speed_kmh") or 0.0) >= _MOVING_SPEED_KMH:
        return True
    return False

async def _insert_local_gps_point(
    *,
    lat: float | None,
    lon: float | None,
    speed: float | None,
    fix_status: str,
) -> None:
    try:
        async with aiosqlite.connect(LOCAL_DB_PATH) as db:
            await db.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute(
                "INSERT INTO local_gps (lat, lon, speed, fix_status) VALUES (?, ?, ?, ?)",
                (lat, lon, speed, fix_status),
            )
            await db.commit()
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("Failed to insert local_gps row into %s: %s", LOCAL_DB_PATH, exc)


async def _parked_scan_cycle(config: Config, state: RuntimeState) -> None:
    """Lean GPS stash + WiFi re-check executed on every parked sleep tick."""
    gps = build_location_payload(get_latest_gps(config, state))
    if gps.get("fix_status") == "locked":
        state.local_sequence += 1
        await _insert_local_gps_point(
            lat=gps.get("latitude"),
            lon=gps.get("longitude"),
            speed=gps.get("speed_kmh"),
            fix_status=gps.get("fix_status", "searching"),
        )

    if _truck_is_active(state):
        logger.info("Parked scan: truck is active — resuming full-rate collection.")
        state.parked_mode = False
        state.park_wake_event.set()
        return

    if await pop_beacon_wake_signal():
        logger.info("Parked scan: BLE key beacon signal found — waking.")
        state.parked_mode = False
        state.park_wake_event.set()
        return

    power = await get_latest_power_snapshot(config.vehicle_id)
    if power and bool(power.get("is_charging")):
        logger.info("Parked scan: charging detected (solar/USB) — waking to sync queued data.")
        state.parked_mode = False
        state.park_wake_event.set()
        return

    if is_network_connected():
        logger.info("Parked scan: WiFi detected — exiting parked mode.")
        state.parked_mode = False
        state.wifi_connected = True
        state.park_wake_event.set()


async def parked_scan_worker(config: Config, state: RuntimeState) -> None:
    while True:
        if not state.parked_mode:
            state.park_wake_event.clear()
            await asyncio.sleep(2)
            continue

        power = await get_latest_power_snapshot(config.vehicle_id)
        sleep_seconds = _adaptive_parked_sleep_seconds(power)
        logger.info("Parked mode: sleeping %.0fs before next scan cycle.", sleep_seconds)
        try:
            await asyncio.wait_for(state.park_wake_event.wait(), timeout=sleep_seconds)
            state.park_wake_event.clear()
            if not state.parked_mode:
                logger.info("Parked mode: exiting (WiFi restored or external wake).")
            else:
                logger.info("Parked mode: motion wake — exiting parked mode.")
                state.parked_mode = False
            # Immediately check WiFi so buffered workers can resume normal cadence.
            if is_network_connected():
                state.wifi_connected = True
                logger.info("Parked wake: WiFi available — exiting low-power mode.")
            continue
        except asyncio.TimeoutError:
            pass

        await _parked_scan_cycle(config, state)


async def gps_collector_worker(config: Config, state: RuntimeState) -> None:
    while True:
        if state.parked_mode:
            await asyncio.sleep(5)
            continue

        captured_at = utc_now_iso()
        gps = build_location_payload(get_latest_gps(config, state))

        if gps.get("fix_status") == "locked":
            state.last_locked_gps_point_utc = captured_at
            state.local_sequence += 1

            await _insert_local_gps_point(
                lat=gps.get("latitude"),
                lon=gps.get("longitude"),
                speed=gps.get("speed_kmh"),
                fix_status=gps.get("fix_status", "searching"),
            )
        else:
            stale_for = age_seconds(state.last_gps_fix_utc)
            state.gps_reader_ok = stale_for is None or stale_for <= 300

        await asyncio.sleep(config.gps_sample_interval_seconds)


async def heartbeat_builder_worker(config: Config, state: RuntimeState, imu: ImuMonitor | None) -> None:
    while True:
        if state.parked_mode:
            await asyncio.sleep(10)
            continue

        captured_at = utc_now_iso()
        gps_payload = build_location_payload(get_latest_gps(config, state))
        state.wifi_connected = is_network_connected()
        db_stats = await get_db_stats()
        if config.device_role == "warehouse":
            power_metrics = {
                "status": "skipped",
                "reason": "device_role_warehouse",
                "source": "disabled_for_role",
                "snapshot_found": False,
                "snapshot_stale": False,
                "snapshot_age_sec": None,
                "snapshot_captured_at_utc": None,
            }
            state.power_monitor_ok = True
        else:
            latest_power_snapshot = await get_latest_power_snapshot(config.vehicle_id)
            power_metrics, state.power_monitor_ok = build_power_metrics_payload(
                latest_power_snapshot,
                max_snapshot_age_seconds=config.power_snapshot_max_age_seconds,
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
                "fix_status": gps_payload.get("fix_status", "searching"),
                "last_fix_age_sec": age_seconds(state.last_gps_fix_utc) if config.device_role != "warehouse" else 0,
            },
            "power_metrics": power_metrics,
            "imu_metrics": imu.read() if imu is not None else {"status": "skipped", "reason": "device_role_warehouse"},
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



async def maintenance_worker(config: Config, state: RuntimeState) -> None:
    while True:
        await asyncio.sleep(60)

        if state.parked_mode:
            continue

        db_stats = await get_db_stats()
        disk = shutil.disk_usage("/")
        disk_free_mb = round(disk.free / (1024 * 1024), 2)

        last_gps_fix_age = age_seconds(state.last_gps_fix_utc)
        queue_depth = db_stats["queue_depth"]

        alerts: list[str] = []
        if config.device_role != "warehouse" and last_gps_fix_age is not None and last_gps_fix_age > 300:
            alerts.append("gps_reader_stale")
            state.gps_reader_ok = False

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
            "last_gps_fix_age_sec": last_gps_fix_age,
            "last_locked_gps_point_age_sec": age_seconds(state.last_locked_gps_point_utc),
            "pending_gps_points": db_stats["pending_gps_points"],
            "pending_heartbeats": db_stats["pending_heartbeats"],
            "wifi_connected": state.wifi_connected,
            "gps_reader_ok": state.gps_reader_ok,
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
                "last_upload_success_utc": None,
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
    warehouse_mode = config.device_role == "warehouse"
    if warehouse_mode:
        logger.info(
            "DEVICE_ROLE=warehouse detected. GPS and power-monitor dependencies disabled; "
            "heartbeats will use configured warehouse coordinates."
        )
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
        probe_serial=not warehouse_mode,
    )
    print(f"Hardware inventory: {inventory.to_json()}")
    os.makedirs("/data", exist_ok=True)
    with open("/data/telematics_hardware_inventory.json", "w", encoding="utf-8") as handle:
        json.dump(inventory.to_dict(), handle, sort_keys=True)
    inventory_errors = validate_inventory(
        inventory,
        imu_required=(config.imu_required and not warehouse_mode),
        ups_required=False,
    )
    if inventory_errors:
        raise RuntimeError(
            "Hardware probe failed: "
            f"{'; '.join(inventory_errors)} "
            "Action: resolve the hardware and environment variable issues above, then restart telematics-edge."
        )

    imu = ImuMonitor(bus_num=config.imu_i2c_bus) if not warehouse_mode else None
    state = RuntimeState(start_monotonic=time.monotonic())

    async def _on_motion_wake() -> None:
        state.last_motion_monotonic = time.monotonic()
        if state.parked_mode:
            logger.info("Motion wake: IMU harsh event detected in parked mode.")
            state.parked_mode = False
            state.park_wake_event.set()

    if imu is not None:
        imu.set_motion_wake_callback(_on_motion_wake)

    await init_db()
    print(f"Starting telematics-edge for vehicle {config.vehicle_id}.")

    tasks = [
        asyncio.create_task(heartbeat_builder_worker(config, state, imu)),
        asyncio.create_task(maintenance_worker(config, state)),
        asyncio.create_task(network_watchdog_worker(config, state)),
    ]
    if not warehouse_mode and imu is not None:
        tasks.extend(
            [
                asyncio.create_task(imu.start()),
                asyncio.create_task(gps_reader_worker(config, state)),
                asyncio.create_task(gps_collector_worker(config, state)),
                asyncio.create_task(parked_scan_worker(config, state)),
            ]
        )
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    uvloop.install()
    asyncio.run(run())
