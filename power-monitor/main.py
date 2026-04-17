import asyncio
import json
import logging
import random
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import aiosqlite
import uvloop
from ina219 import INA219, DeviceRangeError
from smbus2 import SMBus, i2c_msg

sys.path.append(str(Path(__file__).resolve().parents[1]))

from shared.hardware_probe import (
    build_hardware_inventory,
    parse_bool_env,
    parse_hex_list_env,
    parse_int_env,
    validate_inventory,
)

logger = logging.getLogger(__name__)

UPLOADABLE_EVENT_TYPES: tuple[str, ...] = ("power_snapshot", "power_state", "power_health", "power_diagnostic")


def read_i2c_atomic(bus_num: int, address: int, register: int, length: int) -> list[int]:
    """Executes a hardware-locked combined I2C transaction."""
    retries = 3
    for attempt in range(retries):
        try:
            with SMBus(bus_num) as bus:
                write_msg = i2c_msg.write(address, [register])
                read_msg = i2c_msg.read(address, length)
                bus.i2c_rdwr(write_msg, read_msg)
                return list(read_msg)
        except OSError:
            if attempt == retries - 1:
                raise
            time.sleep(0.05)
    raise RuntimeError("I2C read exhausted retries")


class Ina219Adapter:
    _SHUNT_VOLTAGE_REGISTER = 0x01
    _BUS_VOLTAGE_REGISTER = 0x02
    _POWER_REGISTER = 0x03
    _CURRENT_REGISTER = 0x04
    _CNVR_BIT = 2
    _OVF_BIT = 1

    def __init__(self, sensor: INA219, address: int, bus_num: int) -> None:
        self._sensor = sensor
        self.address = address
        self._bus_num = bus_num

    def configure(self, **kwargs: Any) -> None:
        self._sensor.configure(**kwargs)

    def calibration_snapshot(self) -> dict[str, Any]:
        gain_index = getattr(self._sensor, "_gain", None)
        voltage_range_index = getattr(self._sensor, "_voltage_range", None)
        gain_lookup = {
            INA219.GAIN_1_40MV: "gain_1_40mv",
            INA219.GAIN_2_80MV: "gain_2_80mv",
            INA219.GAIN_4_160MV: "gain_4_160mv",
            INA219.GAIN_8_320MV: "gain_8_320mv",
        }
        bus_range_lookup = {
            INA219.RANGE_16V: 16,
            INA219.RANGE_32V: 32,
        }
        gain_max_mv_lookup = {
            INA219.GAIN_1_40MV: 40,
            INA219.GAIN_2_80MV: 80,
            INA219.GAIN_4_160MV: 160,
            INA219.GAIN_8_320MV: 320,
        }
        return {
            "gain_strategy_active": "auto" if bool(getattr(self._sensor, "_auto_gain_enabled", False)) else "fixed",
            "gain_setting": gain_lookup.get(gain_index, "unknown"),
            "gain_max_shunt_mv": gain_max_mv_lookup.get(gain_index),
            "bus_voltage_range_v": bus_range_lookup.get(voltage_range_index),
            "current_lsb_a_per_bit": float(getattr(self._sensor, "_current_lsb", 0.0)),
            "power_lsb_w_per_bit": float(getattr(self._sensor, "_power_lsb", 0.0)),
            "max_expected_amps": getattr(self._sensor, "_max_expected_amps", None),
        }

    def _read_register_atomic(self, register: int, *, signed: bool = False) -> int:
        raw_bytes = read_i2c_atomic(self._bus_num, self.address, register, 2)
        raw_value = (raw_bytes[0] << 8) | raw_bytes[1]
        if signed and raw_value >= 0x8000:
            return raw_value - 0x10000
        return raw_value

    def read(self) -> dict[str, Any]:
        bus_voltage_register = self._read_register_atomic(self._BUS_VOLTAGE_REGISTER)
        shunt_voltage_register = self._read_register_atomic(self._SHUNT_VOLTAGE_REGISTER, signed=True)
        current_register = self._read_register_atomic(self._CURRENT_REGISTER, signed=True)
        power_register = self._read_register_atomic(self._POWER_REGISTER)
        current_lsb = float(getattr(self._sensor, "_current_lsb"))
        power_lsb = float(getattr(self._sensor, "_power_lsb"))

        bus_voltage_v = round(float((bus_voltage_register >> 3) * 4) / 1000.0, 3)
        current_ma = round(float(current_register * current_lsb * 1000.0), 3)
        power_mw = round(float(power_register * power_lsb * 1000.0), 3)
        shunt_voltage_mv = round(float(shunt_voltage_register * 0.01), 3)
        return {
            "bus_voltage_v": bus_voltage_v,
            "current_ma": current_ma,
            "power_mw": power_mw,
            "shunt_voltage_mv": shunt_voltage_mv,
            "cnvr": bool((bus_voltage_register >> self._CNVR_BIT) & 0x1),
            "ovf": bool((bus_voltage_register >> self._OVF_BIT) & 0x1),
            "ina219_address": f"0x{self.address:02X}",
        }


@dataclass(frozen=True)
class Config:
    vehicle_id: str
    db_path: str
    webhook_url: str | None
    api_key: str
    sample_interval_seconds: int
    ina219_addresses: tuple[int, ...]
    i2c_bus: int
    ina219_shunt_ohms: float
    ina219_max_expected_amps: float
    ina219_gain_strategy: str
    ina219_bus_voltage_range_v: int
    battery_capacity_mah: int
    min_discharge_current_ma_for_runtime: int
    upload_batch_size: int
    upload_backoff_initial_seconds: int
    upload_backoff_max_seconds: int
    queue_max_events: int
    maintenance_interval_seconds: int
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


def _read_int_env(name: str, default: int, *, minimum: int | None = None) -> int:
    raw_value = os.getenv(name)
    value = _sanitize_env_value(raw_value)
    if value is None:
        parsed = default
    else:
        try:
            parsed = int(value)
        except ValueError:
            print(f"Warning: invalid {name}={raw_value!r}; using default {default}.")
            parsed = default

    if minimum is not None:
        return max(minimum, parsed)
    return parsed


def _read_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    value = _sanitize_env_value(raw_value)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        print(f"Warning: invalid {name}={raw_value!r}; using default {default}.")
        return default


def _read_ina219_gain_strategy_env(default: str = "auto") -> str:
    raw_value = os.getenv("UPS_GAIN_STRATEGY")
    value = (_sanitize_env_value(raw_value) or default).strip().lower()
    allowed = {"auto", "gain_1_40mv", "gain_2_80mv", "gain_4_160mv", "gain_8_320mv"}
    if value not in allowed:
        print(f"Warning: invalid UPS_GAIN_STRATEGY={raw_value!r}; using default {default}.")
        return default
    return value


def _read_ina219_bus_voltage_range_env(default: int = 32) -> int:
    parsed = _read_int_env("UPS_BUS_VOLTAGE_RANGE_V", default)
    if parsed not in {16, 32}:
        print(f"Warning: invalid UPS_BUS_VOLTAGE_RANGE_V={parsed!r}; using default {default}.")
        return default
    return parsed


def _read_hex_address_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    value = _sanitize_env_value(raw_value)
    if value is None:
        return default
    try:
        return int(value, 16)
    except ValueError:
        print(f"Warning: invalid {name}={raw_value!r}; using default 0x{default:02X}.")
        return default


def _dedupe_preserve_order(values: list[int]) -> tuple[int, ...]:
    seen: set[int] = set()
    deduped: list[int] = []
    for value in values:
        if value in seen:
            continue
        deduped.append(value)
        seen.add(value)
    return tuple(deduped)


def _read_hex_address_list_env(name: str) -> tuple[int, ...]:
    raw_value = os.getenv(name)
    value = _sanitize_env_value(raw_value)
    if value is None:
        return tuple()

    addresses: list[int] = []
    for token in value.split(","):
        item = token.strip()
        if not item:
            continue
        try:
            addresses.append(int(item, 16))
        except ValueError:
            print(f"Warning: invalid {name} entry {item!r}; skipping.")
    return _dedupe_preserve_order(addresses)


def load_config() -> Config:
    primary_address = _read_hex_address_env("UPS_I2C_ADDRESS", 0x43)
    configured_candidates = _read_hex_address_list_env("UPS_I2C_ADDRESS_CANDIDATES")
    fallback_candidates = (0x43, 0x40, 0x41, 0x44, 0x45)
    ina219_addresses = _dedupe_preserve_order([primary_address, *configured_candidates, *fallback_candidates])
    i2c_bus = _read_int_env("I2C_BUS", 1, minimum=0)

    return Config(
        vehicle_id=_sanitize_env_value(os.getenv("VEHICLE_ID")) or "UNKNOWN_TRUCK",
        db_path=_sanitize_env_value(os.getenv("TELEMATICS_DB_PATH")) or "/data/telematics.db",
        webhook_url=_sanitize_env_value(os.getenv("WEBHOOK_URL")),
        api_key=_sanitize_env_value(os.getenv("API_KEY")) or "",
        sample_interval_seconds=_read_int_env("POWER_SAMPLE_INTERVAL_SECONDS", 2, minimum=1),
        ina219_addresses=ina219_addresses,
        i2c_bus=i2c_bus,
        ina219_shunt_ohms=_read_float_env("UPS_SHUNT_OHMS", 0.01),
        ina219_max_expected_amps=max(0.05, _read_float_env("UPS_MAX_EXPECTED_AMPS", 3.2)),
        ina219_gain_strategy=_read_ina219_gain_strategy_env("gain_8_320mv"),
        ina219_bus_voltage_range_v=_read_ina219_bus_voltage_range_env(16),
        battery_capacity_mah=_read_int_env("UPS_BATTERY_CAPACITY_MAH", 2200, minimum=1),
        min_discharge_current_ma_for_runtime=_read_int_env(
            "UPS_MIN_DISCHARGE_CURRENT_MA_FOR_RUNTIME_ESTIMATE",
            20,
            minimum=1,
        ),
        upload_batch_size=_read_int_env("POWER_UPLOAD_BATCH_SIZE", 50, minimum=1),
        upload_backoff_initial_seconds=_read_int_env("POWER_UPLOAD_BACKOFF_INITIAL_SECONDS", 5, minimum=1),
        upload_backoff_max_seconds=_read_int_env("POWER_UPLOAD_BACKOFF_MAX_SECONDS", 300, minimum=10),
        queue_max_events=_read_int_env("POWER_QUEUE_MAX_EVENTS", 2000, minimum=100),
        maintenance_interval_seconds=_read_int_env("POWER_MAINTENANCE_INTERVAL_SECONDS", 30, minimum=10),
        imu_i2c_bus=parse_int_env("IMU_I2C_BUS", i2c_bus, minimum=0),
        imu_expected_addresses=parse_hex_list_env("IMU_EXPECTED_ADDRESSES", (0x6A,)),
        imu_required=False,  # power-monitor does not use the IMU; telematics-edge owns it
    )


class UpsMonitor:
    SOC_ESTIMATE_METHOD = "voltage_curve_loaded"

    def __init__(
        self,
        i2c_addresses: tuple[int, ...],
        shunt_ohms: float,
        max_expected_amps: float,
        gain_strategy: str,
        bus_voltage_range_v: int,
        i2c_bus: int,
        battery_capacity_mah: int,
        min_discharge_current_ma_for_runtime: int,
    ) -> None:
        self._i2c_addresses = i2c_addresses
        self._shunt_ohms = shunt_ohms
        self._max_expected_amps = max_expected_amps
        self._gain_strategy = gain_strategy
        self._bus_voltage_range_v = bus_voltage_range_v
        self._i2c_bus = i2c_bus
        self._battery_capacity_mah = battery_capacity_mah
        self._min_discharge_current_ma_for_runtime = min_discharge_current_ma_for_runtime
        self._ina: Ina219Adapter | None = None
        self.reinitialize()

    def _ina219_configure_kwargs(self) -> dict[str, Any]:
        gain_map = {
            "auto": INA219.GAIN_AUTO,
            "gain_1_40mv": INA219.GAIN_1_40MV,
            "gain_2_80mv": INA219.GAIN_2_80MV,
            "gain_4_160mv": INA219.GAIN_4_160MV,
            "gain_8_320mv": INA219.GAIN_8_320MV,
        }
        voltage_range = INA219.RANGE_16V if self._bus_voltage_range_v == 16 else INA219.RANGE_32V
        return {
            "voltage_range": voltage_range,
            "gain": gain_map.get(self._gain_strategy, INA219.GAIN_AUTO),
            "bus_adc": INA219.ADC_12BIT,
            "shunt_adc": INA219.ADC_12BIT,
        }

    def build_overflow_diagnostic(self) -> dict[str, Any]:
        calibration = self._ina.calibration_snapshot() if self._ina else {}
        return {
            "diagnostic_type": "ina219_overflow",
            "shunt_ohms": self._shunt_ohms,
            "expected_max_amps": self._max_expected_amps,
            "gain_strategy_requested": self._gain_strategy,
            "bus_voltage_range_requested_v": self._bus_voltage_range_v,
            "calibration": calibration,
        }

    def reinitialize(self) -> bool:
        self._ina = None
        last_error: str | None = None
        candidate_list = ", ".join(f"0x{address:02X}" for address in self._i2c_addresses)
        print(
            "UPS monitor startup probe: "
            f"bus={self._i2c_bus} "
            f"addresses=[{candidate_list}]"
        )
        configure_kwargs = self._ina219_configure_kwargs()
        print(
            "UPS INA219 configuration: "
            f"expected_max_amps={self._max_expected_amps}A "
            f"shunt_ohms={self._shunt_ohms} "
            f"gain_strategy={self._gain_strategy} "
            f"bus_voltage_range={self._bus_voltage_range_v}V "
            "bus_adc=12bit shunt_adc=12bit"
        )
        for address in self._i2c_addresses:
            try:
                self._ina = Ina219Adapter(
                    INA219(
                        shunt_ohms=self._shunt_ohms,
                        max_expected_amps=self._max_expected_amps,
                        address=address,
                        busnum=self._i2c_bus,
                    ),
                    address,
                    self._i2c_bus,
                )
                self._ina.configure(**configure_kwargs)
                print(f"UPS monitor initialized. bus={self._i2c_bus} address=0x{address:02X}.")
                return True
            except Exception as exc:
                last_error = str(exc)
                print(f"Warning: UPS monitor unavailable. bus={self._i2c_bus} address=0x{address:02X} error={exc}")

        if last_error:
            print(
                "Warning: UPS monitor unavailable on all candidate I2C addresses. "
                f"bus={self._i2c_bus} addresses=[{candidate_list}]"
            )
        return False

    def calibrate(self) -> bool:
        if not self._ina:
            return False
        try:
            self._ina.configure(**self._ina219_configure_kwargs())
            return True
        except Exception:
            return False

    @staticmethod
    def _estimate_soc(voltage_v: float) -> int:
        if voltage_v >= 3.87:
            return 100
        if voltage_v >= 3.7:
            return 75
        if voltage_v >= 3.55:
            return 50
        if voltage_v >= 3.4:
            return 25
        return 0

    def read(self) -> dict[str, Any]:
        read_started = asyncio.get_running_loop().time()

        def _finalize(payload: dict[str, Any]) -> dict[str, Any]:
            payload["read_latency_ms"] = round((asyncio.get_running_loop().time() - read_started) * 1000, 2)
            payload["read_status"] = str(payload.get("status", "unknown"))
            return payload

        if not self._ina:
            return _finalize({"status": "offline"})

        try:
            metrics = self._ina.read()
            if bool(metrics.get("ovf")):
                metrics["overflow_diagnostic"] = self.build_overflow_diagnostic()
            current_ma = float(metrics["current_ma"])
            state_of_charge_pct_estimate = self._estimate_soc(float(metrics["bus_voltage_v"]))
            estimated_runtime_hours: float | None = None
            discharge_current_ma = abs(current_ma)
            if current_ma < -self._min_discharge_current_ma_for_runtime:
                remaining_capacity_mah = (state_of_charge_pct_estimate / 100.0) * float(self._battery_capacity_mah)
                estimated_runtime_hours = round(remaining_capacity_mah / discharge_current_ma, 2)
            return _finalize({
                "status": "ok",
                **metrics,
                "state_of_charge_pct_estimate": state_of_charge_pct_estimate,
                "battery_capacity_mah": self._battery_capacity_mah,
                "estimated_runtime_hours": estimated_runtime_hours,
                "estimate_method": self.SOC_ESTIMATE_METHOD,
                "is_charging": current_ma > 0,
            })
        except DeviceRangeError:
            return _finalize({"status": "range_error"})
        except Exception as exc:
            return _finalize({"status": "read_error", "message": str(exc)})




async def configure_sqlite(conn: aiosqlite.Connection) -> None:
    """Apply low-overhead SQLite settings for SD-card backed storage."""
    await conn.execute("PRAGMA busy_timeout=20000;")
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA synchronous=NORMAL;")
    await conn.execute("PRAGMA temp_store=MEMORY;")

async def init_db(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS power_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            synced INTEGER DEFAULT 0,
            upload_attempts INTEGER DEFAULT 0,
            last_error TEXT
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_events_sync_id ON events(synced, id)")
    await _ensure_column(conn, "events", "upload_attempts", "INTEGER DEFAULT 0")
    await _ensure_column(conn, "events", "last_error", "TEXT")
    await conn.commit()


async def _ensure_column(conn: aiosqlite.Connection, table_name: str, column_name: str, column_def: str) -> None:
    try:
        await conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
    except aiosqlite.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


async def write_reading(
    conn: aiosqlite.Connection,
    vehicle_id: str,
    occurred_at: str,
    payload: dict[str, Any],
) -> None:
    json_payload = json.dumps(payload, separators=(",", ":"))
    await conn.execute(
        "INSERT INTO power_readings(vehicle_id, occurred_at, payload) VALUES(?, ?, ?)",
        (vehicle_id, occurred_at, json_payload),
    )


async def enqueue_event(
    conn: aiosqlite.Connection,
    vehicle_id: str,
    event_type: str,
    occurred_at: str,
    payload: dict[str, Any],
    queue_max_events: int,
) -> None:
    json_payload = json.dumps(payload, separators=(",", ":"))
    await conn.execute(
        "INSERT INTO events(vehicle_id, event_type, occurred_at, payload, synced) VALUES(?, ?, ?, ?, 0)",
        (vehicle_id, event_type, occurred_at, json_payload),
    )
    await conn.execute(
        """
        DELETE FROM events
        WHERE id IN (
            SELECT id FROM events
            WHERE synced = 0
            ORDER BY id ASC
            LIMIT (
                SELECT CASE WHEN COUNT(*) > ? THEN COUNT(*) - ? ELSE 0 END FROM events WHERE synced = 0
            )
        )
        """,
        (queue_max_events, queue_max_events),
    )
    await conn.commit()


async def get_pending_events(
    conn: aiosqlite.Connection,
    limit: int,
    event_types: tuple[str, ...] = UPLOADABLE_EVENT_TYPES,
) -> list[tuple[int, str, str, str, str]]:
    placeholders = ",".join("?" for _ in event_types)
    cursor = await conn.execute(
        f"""
        SELECT id, vehicle_id, event_type, occurred_at, payload
        FROM events
        WHERE synced = 0
          AND event_type IN ({placeholders})
        ORDER BY id ASC
        LIMIT ?
        """,
        (*event_types, limit),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [(int(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4])) for row in rows]


async def mark_event_uploaded(conn: aiosqlite.Connection, event_id: int) -> None:
    await conn.execute("UPDATE events SET synced = 1, last_error = NULL WHERE id = ?", (event_id,))
    await conn.commit()


async def mark_event_failed(conn: aiosqlite.Connection, event_id: int, error_text: str) -> None:
    await conn.execute(
        "UPDATE events SET upload_attempts = upload_attempts + 1, last_error = ? WHERE id = ?",
        (error_text[:300], event_id),
    )
    await conn.commit()


async def publish_event(
    session: aiohttp.ClientSession,
    webhook_url: str | None,
    api_key: str,
    event_type: str,
    vehicle_id: str,
    occurred_at: str,
    payload: dict[str, Any],
) -> tuple[bool, str | None]:
    if not webhook_url:
        return False, "webhook_not_configured"

    body = {
        "event_type": event_type,
        "vehicle_id": vehicle_id,
        "occurred_at": occurred_at,
        "payload": payload,
    }
    if event_type == "power_snapshot":
        body["power_metrics"] = payload
    headers = {"X-Api-Key": api_key} if api_key else {}
    try:
        async with session.post(webhook_url, json=body, headers=headers) as response:
            if response.status >= 400:
                response_text = (await response.text())[:300]
                return False, f"http_{response.status}:{response_text}"
            return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}:{exc}"


async def drain_upload_queue(config: Config, conn: aiosqlite.Connection, session: aiohttp.ClientSession) -> tuple[bool, int]:
    pending = await get_pending_events(conn, config.upload_batch_size, UPLOADABLE_EVENT_TYPES)
    if not pending:
        return True, 0

    for event_id, vehicle_id, event_type, occurred_at, payload_json in pending:
        payload = json.loads(payload_json)
        success, error_text = await publish_event(
            session=session,
            webhook_url=config.webhook_url,
            api_key=config.api_key,
            event_type=event_type,
            vehicle_id=vehicle_id,
            occurred_at=occurred_at,
            payload=payload,
        )
        if success:
            await mark_event_uploaded(conn, event_id)
            continue

        reason = error_text or "unknown_error"
        await mark_event_failed(conn, event_id, reason)
        print(
            "Power event upload error: "
            f"event_id={event_id} event_type={event_type} queue_depth={len(pending)} reason={reason}"
        )
        return False, len(pending)

    return True, len(pending)


@dataclass
class RuntimeStats:
    last_sensor_read_at: str | None = None
    last_successful_read_at: str | None = None
    last_sensor_latency_ms: float | None = None
    last_sensor_status: str | None = None
    last_sensor_marker: str | None = None
    consecutive_read_failures: int = 0
    sensor_reinit_count: int = 0
    sensor_reinit_attempts: int = 0
    sensor_reinit_successes: int = 0
    state_transitions: int = 0
    uploader_failure_streak: int = 0
    last_upload_ok_at: str | None = None
    last_upload_attempt_at: str | None = None


def _derive_power_flags(payload: dict[str, Any]) -> dict[str, bool]:
    status = payload.get("read_status", payload.get("status"))
    sensor_fault = status != "ok"
    overflow_fault = bool(payload.get("ovf"))
    current_ma = float(payload.get("current_ma", 0.0))
    bus_voltage_v = float(payload.get("bus_voltage_v", 0.0))
    external_input_present = (not sensor_fault) and bus_voltage_v >= 4.5
    charging = (not sensor_fault) and current_ma > 20
    discharging = (not sensor_fault) and current_ma < -20
    battery_only = (not sensor_fault) and (not external_input_present)
    brownout_risk = (not sensor_fault) and bus_voltage_v <= 3.45

    return {
        "external_input_present": external_input_present,
        "battery_only": battery_only,
        "charging": charging,
        "discharging": discharging,
        "brownout_risk": brownout_risk,
        "sensor_fault": sensor_fault,
        "overflow_fault": overflow_fault,
    }


def _derive_power_state(flags: dict[str, bool]) -> str:
    if flags["sensor_fault"]:
        return "sensor_fault"
    if flags["overflow_fault"]:
        return "overflow_fault"
    if flags["brownout_risk"]:
        return "brownout_risk"
    if flags["charging"]:
        return "charging"
    if flags["discharging"]:
        return "discharging"
    if flags["battery_only"]:
        return "battery_only"
    if flags["external_input_present"]:
        return "external_input_present"
    return "idle"


async def sensor_loop(config: Config, monitor: UpsMonitor, out_queue: asyncio.Queue[dict[str, Any]], stats: RuntimeStats) -> None:
    while True:
        started = asyncio.get_running_loop().time()
        occurred_at = datetime.now(timezone.utc).isoformat()
        payload = monitor.read()
        latency_ms = round((asyncio.get_running_loop().time() - started) * 1000, 2)
        stats.last_sensor_read_at = occurred_at
        stats.last_sensor_latency_ms = latency_ms
        status = str(payload.get("status", "unknown"))

        # Recovery ladder:
        # 1) transient read retry
        # 2) INA219 reconfigure/recalibrate
        # 3) I2C re-init of monitor object
        # 4) keep explicit sensor fault marker if still failing
        recovered_after_reinit = False
        recovery_steps: list[str] = []
        if status in {"range_error", "read_error", "offline"}:
            retry_payload = monitor.read()
            retry_status = str(retry_payload.get("status", "unknown"))
            recovery_steps.append("transient_retry")
            if retry_status == "ok":
                payload = retry_payload
                status = retry_status
                recovery_steps.append("retry_success")
            else:
                calibrated = monitor.calibrate()
                recovery_steps.append("reconfigure_recalibrate")
                if calibrated:
                    recalibrated_payload = monitor.read()
                    recalibrated_status = str(recalibrated_payload.get("status", "unknown"))
                    if recalibrated_status == "ok":
                        payload = recalibrated_payload
                        status = recalibrated_status
                        recovery_steps.append("recalibration_success")
                if status != "ok":
                    stats.sensor_reinit_attempts += 1
                    recovery_steps.append("i2c_reinitialize")
                    if monitor.reinitialize():
                        stats.sensor_reinit_successes += 1
                        stats.sensor_reinit_count += 1
                        reinit_payload = monitor.read()
                        reinit_status = str(reinit_payload.get("status", "unknown"))
                        if reinit_status == "ok":
                            payload = reinit_payload
                            status = reinit_status
                            recovered_after_reinit = True
                            recovery_steps.append("reinitialize_success")

        marker_map = {
            "offline": "sensor_offline",
            "read_error": "read_error",
            "range_error": "range_error",
        }
        sensor_status_marker = "recovered_after_reinit" if recovered_after_reinit else marker_map.get(status, "ok")
        payload["sensor_status_marker"] = sensor_status_marker
        if recovery_steps:
            payload["recovery_steps"] = recovery_steps

        stats.last_sensor_status = status
        stats.last_sensor_marker = sensor_status_marker
        if status == "ok":
            stats.last_successful_read_at = occurred_at
            stats.consecutive_read_failures = 0
        else:
            stats.consecutive_read_failures += 1
            payload["sensor_alert"] = True

        event = {"occurred_at": occurred_at, "payload": payload, "sensor_latency_ms": latency_ms}
        await out_queue.put(event)
        await asyncio.sleep(config.sample_interval_seconds)


async def state_engine_loop(
    config: Config,
    conn: aiosqlite.Connection,
    in_queue: asyncio.Queue[dict[str, Any]],
    stats: RuntimeStats,
) -> None:
    previous_state: str | None = None
    previous_flags: dict[str, bool] | None = None
    while True:
        event = await in_queue.get()
        occurred_at = str(event["occurred_at"])
        payload = dict(event["payload"])
        payload["sensor_latency_ms"] = event["sensor_latency_ms"]
        current_flags = _derive_power_flags(payload)
        payload.update(current_flags)
        current_state = _derive_power_state(current_flags)
        payload["power_state"] = current_state

        await write_reading(conn, config.vehicle_id, occurred_at, payload)
        await enqueue_event(conn, config.vehicle_id, "power_snapshot", occurred_at, payload, config.queue_max_events)
        if bool(payload.get("ovf")):
            overflow_diagnostic_payload = {
                "state": current_state,
                "flags": current_flags,
                "diagnostic": payload.get("overflow_diagnostic", {}),
                "reading": {
                    "ina219_address": payload.get("ina219_address"),
                    "bus_voltage_v": payload.get("bus_voltage_v"),
                    "shunt_voltage_mv": payload.get("shunt_voltage_mv"),
                    "current_ma": payload.get("current_ma"),
                    "power_mw": payload.get("power_mw"),
                },
            }
            await enqueue_event(
                conn,
                config.vehicle_id,
                "power_diagnostic",
                occurred_at,
                overflow_diagnostic_payload,
                config.queue_max_events,
            )

        if previous_state is not None and current_state != previous_state:
            stats.state_transitions += 1
            transition_payload = {
                "state": current_state,
                "previous_state": previous_state,
                "flags": current_flags,
            }
            await enqueue_event(
                conn,
                config.vehicle_id,
                "power_state",
                occurred_at,
                transition_payload,
                config.queue_max_events,
            )
            print(f"Power state transition: {previous_state} -> {current_state} at {occurred_at}")

        if previous_flags is None or any(previous_flags.get(key) != value for key, value in current_flags.items()):
            health_payload = {
                "state": current_state,
                "previous_state": previous_state,
                "flags": current_flags,
                "previous_flags": previous_flags or {},
            }
            await enqueue_event(
                conn,
                config.vehicle_id,
                "power_health",
                occurred_at,
                health_payload,
                config.queue_max_events,
            )

        previous_flags = current_flags
        previous_state = current_state
        await conn.commit()
        in_queue.task_done()
        print(f"Stored power reading: {payload.get('status')} state={current_state} at {occurred_at}")


async def uploader_loop(config: Config, conn: aiosqlite.Connection, session: aiohttp.ClientSession, stats: RuntimeStats) -> None:
    upload_backoff_seconds = config.upload_backoff_initial_seconds
    while True:
        stats.last_upload_attempt_at = datetime.now(timezone.utc).isoformat()
        uploaded, queue_depth = await drain_upload_queue(config, conn, session)
        if uploaded:
            upload_backoff_seconds = config.upload_backoff_initial_seconds
            stats.uploader_failure_streak = 0
            stats.last_upload_ok_at = datetime.now(timezone.utc).isoformat()
            await asyncio.sleep(1)
            continue

        stats.uploader_failure_streak += 1
        jitter = random.uniform(0, min(1.0, upload_backoff_seconds * 0.2))
        upload_backoff_seconds = min(
            config.upload_backoff_max_seconds,
            max(config.upload_backoff_initial_seconds, upload_backoff_seconds * 2),
        )
        delay = upload_backoff_seconds + jitter
        print(
            "Uploader backoff active: "
            f"next_delay={round(delay, 2)}s queue_depth={queue_depth}"
        )
        await asyncio.sleep(delay)


async def maintenance_loop(config: Config, conn: aiosqlite.Connection, stats: RuntimeStats) -> None:
    while True:
        queue_depth_cursor = await conn.execute("SELECT COUNT(*) FROM events WHERE synced = 0")
        queue_depth_row = await queue_depth_cursor.fetchone()
        await queue_depth_cursor.close()
        queue_depth = int(queue_depth_row[0]) if queue_depth_row else 0

        age_cursor = await conn.execute(
            "SELECT occurred_at FROM events WHERE synced = 0 ORDER BY id ASC LIMIT 1"
        )
        age_row = await age_cursor.fetchone()
        await age_cursor.close()
        oldest_upload_age_seconds: float | None = None
        if age_row and age_row[0]:
            oldest = datetime.fromisoformat(str(age_row[0]))
            oldest_upload_age_seconds = (datetime.now(timezone.utc) - oldest).total_seconds()

        sensor_staleness_seconds: float | None = None
        if stats.last_sensor_read_at:
            last_sensor_dt = datetime.fromisoformat(stats.last_sensor_read_at)
            sensor_staleness_seconds = (datetime.now(timezone.utc) - last_sensor_dt).total_seconds()

        print(
            "Maintenance metrics: "
            f"sensor_status={stats.last_sensor_status} "
            f"sensor_staleness_s={round(sensor_staleness_seconds, 2) if sensor_staleness_seconds is not None else 'n/a'} "
            f"sensor_latency_ms={stats.last_sensor_latency_ms if stats.last_sensor_latency_ms is not None else 'n/a'} "
            f"reinit={stats.sensor_reinit_successes}/{stats.sensor_reinit_attempts} "
            f"state_transitions={stats.state_transitions} "
            f"uploader_failure_streak={stats.uploader_failure_streak} "
            f"queue_depth={queue_depth} "
            f"oldest_upload_age_s={round(oldest_upload_age_seconds, 2) if oldest_upload_age_seconds is not None else 'n/a'} "
            f"last_upload_ok_at={stats.last_upload_ok_at or 'n/a'}"
        )
        await asyncio.sleep(config.maintenance_interval_seconds)


async def health_emitter_loop(config: Config, conn: aiosqlite.Connection, stats: RuntimeStats) -> None:
    while True:
        occurred_at = datetime.now(timezone.utc).isoformat()
        queue_depth_cursor = await conn.execute("SELECT COUNT(*) FROM events WHERE synced = 0")
        queue_depth_row = await queue_depth_cursor.fetchone()
        await queue_depth_cursor.close()
        queue_depth = int(queue_depth_row[0]) if queue_depth_row else 0

        last_upload_success_age_sec: float | None = None
        if stats.last_upload_ok_at:
            last_upload_ok_dt = datetime.fromisoformat(stats.last_upload_ok_at)
            last_upload_success_age_sec = max(0.0, (datetime.now(timezone.utc) - last_upload_ok_dt).total_seconds())

        health_payload: dict[str, Any] = {
            "queue_depth": queue_depth,
            "consecutive_read_failures": stats.consecutive_read_failures,
            "last_upload_success_age_sec": round(last_upload_success_age_sec, 2) if last_upload_success_age_sec is not None else None,
            "sensor_reinit_count": stats.sensor_reinit_count,
            "sensor_status_marker": stats.last_sensor_marker or "unknown",
            "sensor_status": stats.last_sensor_status or "unknown",
        }
        if stats.last_successful_read_at:
            last_successful_read_dt = datetime.fromisoformat(stats.last_successful_read_at)
            health_payload["last_successful_read_age_sec"] = round(
                max(0.0, (datetime.now(timezone.utc) - last_successful_read_dt).total_seconds()),
                2,
            )

        if stats.consecutive_read_failures > 0:
            health_payload["sensor_fault"] = True

        await enqueue_event(
            conn,
            config.vehicle_id,
            "power_health",
            occurred_at,
            health_payload,
            config.queue_max_events,
        )
        await asyncio.sleep(60)


async def run() -> None:
    if os.getenv("DEVICE_ROLE", "truck").lower() == "warehouse":
        logger.info("DEVICE_ROLE is set to warehouse. Disabling UPS hardware probes.")
        logger.info("Power Monitor container going into idle sleep mode.")
        while True:
            await asyncio.sleep(86400)

    config = load_config()
    inventory = build_hardware_inventory(
        gps_candidates=(),  # power-monitor does not own the GPS serial port
        gps_baud_rate=9600,
        i2c_bus=config.i2c_bus,
        ups_expected_addresses=config.ina219_addresses,
        imu_expected_addresses=(),  # power-monitor does not own the IMU
        probe_serial=False,
    )
    print(f"Hardware inventory: {inventory.to_json()}")
    os.makedirs("/data", exist_ok=True)
    with open("/data/power_hardware_inventory.json", "w", encoding="utf-8") as handle:
        json.dump(inventory.to_dict(), handle, sort_keys=True)
    inventory_errors = validate_inventory(inventory, imu_required=False, ups_required=True)
    if inventory_errors:
        raise RuntimeError(f"Hardware probe failed: {'; '.join(inventory_errors)}")

    monitor = UpsMonitor(
        config.ina219_addresses,
        config.ina219_shunt_ohms,
        config.ina219_max_expected_amps,
        config.ina219_gain_strategy,
        config.ina219_bus_voltage_range_v,
        config.i2c_bus,
        config.battery_capacity_mah,
        config.min_discharge_current_ma_for_runtime,
    )
    stats = RuntimeStats()
    sensor_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max(1, config.queue_max_events // 2))

    async with aiosqlite.connect(config.db_path, timeout=20.0) as conn:
        await configure_sqlite(conn)
        await init_db(conn)
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(sensor_loop(config, monitor, sensor_queue, stats))
                task_group.create_task(state_engine_loop(config, conn, sensor_queue, stats))
                task_group.create_task(uploader_loop(config, conn, session, stats))
                task_group.create_task(maintenance_loop(config, conn, stats))
                task_group.create_task(health_emitter_loop(config, conn, stats))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    uvloop.install()
    asyncio.run(run())
