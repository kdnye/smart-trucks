import asyncio
import json
import logging
import random
import os
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import aiosqlite
import uvloop
from gpiozero import DigitalOutputDevice
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

UPLOADABLE_EVENT_TYPES: tuple[str, ...] = (
    "power_snapshot",
    "power_state",
    "power_health",
    "power_diagnostic",
    "power_event",
)
SQLITE_CONNECT_TIMEOUT_SECONDS = float(os.getenv("SQLITE_CONNECT_TIMEOUT_SECONDS", "30"))
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "30000"))
DEFAULT_UPS_MAX_EXPECTED_AMPS = 4.0
CURRENT_FLOW_DEADBAND_MA = 20.0
EXTERNAL_INPUT_PRESENT_MIN_BUS_V = 4.5

class AsyncINA219:
    _CONFIG_REGISTER = 0x00
    _SHUNT_VOLTAGE_REGISTER = 0x01
    _BUS_VOLTAGE_REGISTER = 0x02
    _CNVR_MASK = 0x0004
    _OVF_MASK = 0x0002
    _BADC_128_SAMPLES = 0b1111
    _SADC_128_SAMPLES = 0b1111
    _MODE_TRIGGERED_SHUNT_BUS = 0b011
    _VOLTAGE_SOC_MAX_V = 4.20
    _VOLTAGE_SOC_MIN_V = 3.20

    def __init__(
        self,
        address: int,
        bus_num: int,
        *,
        shunt_ohms: float,
        bus_voltage_range_v: int,
        gain_strategy: str,
        scl_gpio_pin: int = 3,
        q: float = 0.05,
        r: float = 100.0,
        p: float = 1.0,
        x: float = 0.0,
    ) -> None:
        self.address = address
        self._bus_num = bus_num
        self._scl_gpio_pin = scl_gpio_pin
        self._bus_voltage_range_v = bus_voltage_range_v
        self._gain_strategy = gain_strategy
        self._bus: SMBus | None = None
        self._io_lock = asyncio.Lock()
        self._m = 1.0 / max(1e-6, shunt_ohms)
        self._c = 0.0
        self.Q = q
        self.R = r
        self.P = p
        self.K = 0.0
        self.X = x
        self.total_mAh = 0.0
        self._last_perf_counter = time.perf_counter()
        self._open_bus()

    def set_calibration_profile(self, m: float, c: float) -> None:
        self._m = float(m)
        self._c = float(c)

    def calibration_snapshot(self) -> dict[str, Any]:
        return {
            "gain_strategy_active": self._gain_strategy,
            "gain_setting": self._gain_strategy,
            "bus_voltage_range_v": self._bus_voltage_range_v,
            "regression_slope_m": self._m,
            "regression_offset_c": self._c,
            "kalman_q": self.Q,
            "kalman_r": self.R,
            "kalman_p": self.P,
            "kalman_x": self.X,
            "total_mAh": self.total_mAh,
        }

    def _open_bus(self) -> None:
        if self._bus is None:
            self._bus = SMBus(self._bus_num)

    def _close_bus(self) -> None:
        if self._bus is not None:
            self._bus.close()
            self._bus = None

    async def _recover_i2c_bus(self) -> None:
        logger.warning("I2C bus fault detected, running 9-pulse bus clear on GPIO%d", self._scl_gpio_pin)
        self._close_bus()
        scl = DigitalOutputDevice(self._scl_gpio_pin, active_high=True, initial_value=True)
        try:
            for _ in range(9):
                scl.on()
                await asyncio.sleep(0.01)
                scl.off()
                await asyncio.sleep(0.01)
            scl.on()
            await asyncio.sleep(0.01)
        finally:
            scl.close()
        self._open_bus()

    async def _i2c_rdwr(self, write_bytes: list[int], read_len: int = 0) -> list[int]:
        async with self._io_lock:
            for attempt in range(2):
                try:
                    self._open_bus()
                    assert self._bus is not None
                    write_msg = i2c_msg.write(self.address, write_bytes)
                    if read_len <= 0:
                        await asyncio.to_thread(self._bus.i2c_rdwr, write_msg)
                        return []
                    read_msg = i2c_msg.read(self.address, read_len)
                    await asyncio.to_thread(self._bus.i2c_rdwr, write_msg, read_msg)
                    return list(read_msg)
                except OSError:
                    if attempt == 0:
                        await self._recover_i2c_bus()
                        continue
                    raise
        raise RuntimeError("I2C transaction failed")

    async def _write_register(self, register: int, value: int) -> None:
        await self._i2c_rdwr([register, (value >> 8) & 0xFF, value & 0xFF], read_len=0)

    async def _read_register(self, register: int, *, signed: bool = False) -> int:
        raw_bytes = await self._i2c_rdwr([register], read_len=2)
        raw_value = (raw_bytes[0] << 8) | raw_bytes[1]
        if signed and raw_value >= 0x8000:
            return raw_value - 0x10000
        return raw_value

    def _build_trigger_config(self) -> int:
        brng = 0 if self._bus_voltage_range_v == 16 else 1
        pga_map = {
            "gain_1_40mv": 0b00,
            "gain_2_80mv": 0b01,
            "gain_4_160mv": 0b10,
            "gain_8_320mv": 0b11,
            "auto": 0b11,
        }
        pga = pga_map.get(self._gain_strategy, 0b11)
        return (
            (brng << 13)
            | (pga << 11)
            | (self._BADC_128_SAMPLES << 7)
            | (self._SADC_128_SAMPLES << 3)
            | self._MODE_TRIGGERED_SHUNT_BUS
        )

    async def trigger_and_fetch(self) -> tuple[int, int, int]:
        await self._write_register(self._CONFIG_REGISTER, self._build_trigger_config())
        while True:
            bus_voltage_register = await self._read_register(self._BUS_VOLTAGE_REGISTER)
            if bus_voltage_register & self._CNVR_MASK:
                shunt_voltage_register = await self._read_register(self._SHUNT_VOLTAGE_REGISTER, signed=True)
                return shunt_voltage_register, bus_voltage_register, bus_voltage_register
            await asyncio.sleep(0.01)

    def _kalman_filter(self, measurement_a: float) -> float:
        self.P = self.P + self.Q
        self.K = self.P / (self.P + self.R)
        self.X = self.X + self.K * (measurement_a - self.X)
        self.P = (1.0 - self.K) * self.P
        return self.X

    def _update_coulomb_counter(self, filtered_current_a: float) -> int:
        now = time.perf_counter()
        delta_us = int((now - self._last_perf_counter) * 1_000_000)
        self._last_perf_counter = now
        self.total_mAh += filtered_current_a * 1000.0 * (float(delta_us) / 3_600_000_000.0)
        return delta_us

    @staticmethod
    def _raw_bus_to_voltage(bus_raw: int) -> float:
        return float((bus_raw >> 3) * 4) / 1000.0

    def estimate_starting_mah(self, current_voltage_v: float, max_capacity_mah: float = 1500.0) -> float:
        """
        Estimate initial battery mAh from cell voltage for one-time startup initialization.
        """
        if current_voltage_v >= self._VOLTAGE_SOC_MAX_V:
            return max_capacity_mah
        if current_voltage_v <= self._VOLTAGE_SOC_MIN_V:
            return 0.0
        soc_percentage = (current_voltage_v - self._VOLTAGE_SOC_MIN_V) / (
            self._VOLTAGE_SOC_MAX_V - self._VOLTAGE_SOC_MIN_V
        )
        return max_capacity_mah * soc_percentage

    async def read(self) -> dict[str, Any]:
        shunt_raw, bus_raw, status_raw = await self.trigger_and_fetch()
        shunt_voltage_v = float(shunt_raw) * 0.00001
        shunt_voltage_mv = shunt_voltage_v * 1000.0
        bus_voltage_v = self._raw_bus_to_voltage(bus_raw)
        current_a = (shunt_voltage_v * self._m) + self._c
        filtered_current_a = self._kalman_filter(current_a)
        delta_us = self._update_coulomb_counter(filtered_current_a)
        current_ma = filtered_current_a * 1000.0
        power_mw = bus_voltage_v * current_ma
        return {
            "bus_voltage_v": round(bus_voltage_v, 3),
            "current_ma": round(current_ma, 3),
            "power_mw": round(power_mw, 3),
            "shunt_voltage_mv": round(shunt_voltage_mv, 3),
            "cnvr": bool(status_raw & self._CNVR_MASK),
            "ovf": bool(status_raw & self._OVF_MASK),
            "ina219_address": f"0x{self.address:02X}",
            "total_mAh": round(self.total_mAh, 6),
            "integration_delta_us": delta_us,
            "raw_current_a": round(current_a, 6),
            "kalman_current_a": round(filtered_current_a, 6),
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
    ina219_scl_gpio_pin: int
    battery_capacity_mah: int
    min_discharge_current_ma_for_runtime: int
    shutdown_soc_trip_pct: float
    shutdown_soc_recover_pct: float
    shutdown_voltage_trip_v: float
    shutdown_voltage_recover_v: float
    shutdown_consecutive_samples: int
    shutdown_command_timeout_seconds: float
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


def _read_float_env_alias(name: str, legacy_name: str, default: float) -> float:
    if _sanitize_env_value(os.getenv(name)) is not None:
        return _read_float_env(name, default)
    return _read_float_env(legacy_name, default)


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

    shutdown_soc_trip_pct = _read_float_env_alias("UPS_SHUTDOWN_SOC_TRIP_PCT", "UPS_SHUTDOWN_SOC_PCT", 10.0)
    shutdown_soc_recover_pct = _read_float_env("UPS_SHUTDOWN_SOC_RECOVER_PCT", max(shutdown_soc_trip_pct + 5.0, 15.0))
    shutdown_voltage_trip_v = _read_float_env_alias("UPS_SHUTDOWN_VOLTAGE_TRIP_V", "UPS_SHUTDOWN_VOLTAGE_V", 3.50)
    shutdown_voltage_recover_v = _read_float_env(
        "UPS_SHUTDOWN_VOLTAGE_RECOVER_V",
        max(shutdown_voltage_trip_v + 0.10, 3.60),
    )

    if shutdown_soc_recover_pct < shutdown_soc_trip_pct:
        print(
            "Warning: UPS_SHUTDOWN_SOC_RECOVER_PCT is below trip threshold; "
            "clamping to trip threshold."
        )
        shutdown_soc_recover_pct = shutdown_soc_trip_pct
    if shutdown_voltage_recover_v < shutdown_voltage_trip_v:
        print(
            "Warning: UPS_SHUTDOWN_VOLTAGE_RECOVER_V is below trip threshold; "
            "clamping to trip threshold."
        )
        shutdown_voltage_recover_v = shutdown_voltage_trip_v

    return Config(
        vehicle_id=_sanitize_env_value(os.getenv("VEHICLE_ID")) or "UNKNOWN_TRUCK",
        db_path=_sanitize_env_value(os.getenv("TELEMATICS_DB_PATH")) or "/data/telematics.db",
        webhook_url=_sanitize_env_value(os.getenv("WEBHOOK_URL")),
        api_key=_sanitize_env_value(os.getenv("API_KEY")) or "",
        sample_interval_seconds=_read_int_env("POWER_SAMPLE_INTERVAL_SECONDS", 2, minimum=1),
        ina219_addresses=ina219_addresses,
        i2c_bus=i2c_bus,
        ina219_shunt_ohms=_read_float_env("UPS_SHUNT_OHMS", 0.01),
        ina219_max_expected_amps=max(
            0.05,
            _read_float_env("UPS_MAX_EXPECTED_AMPS", DEFAULT_UPS_MAX_EXPECTED_AMPS),
        ),
        ina219_gain_strategy=_read_ina219_gain_strategy_env("gain_8_320mv"),
        ina219_bus_voltage_range_v=_read_ina219_bus_voltage_range_env(16),
        ina219_scl_gpio_pin=_read_int_env("UPS_I2C_SCL_GPIO_PIN", 3, minimum=0),
        battery_capacity_mah=_read_int_env("UPS_BATTERY_CAPACITY_MAH", 2200, minimum=1),
        min_discharge_current_ma_for_runtime=_read_int_env(
            "UPS_MIN_DISCHARGE_CURRENT_MA_FOR_RUNTIME_ESTIMATE",
            20,
            minimum=1,
        ),
        shutdown_soc_trip_pct=shutdown_soc_trip_pct,
        shutdown_soc_recover_pct=shutdown_soc_recover_pct,
        shutdown_voltage_trip_v=shutdown_voltage_trip_v,
        shutdown_voltage_recover_v=shutdown_voltage_recover_v,
        shutdown_consecutive_samples=_read_int_env("UPS_SHUTDOWN_CONSECUTIVE_SAMPLES", 3, minimum=1),
        shutdown_command_timeout_seconds=max(
            1.0,
            _read_float_env("UPS_SHUTDOWN_COMMAND_TIMEOUT_SECONDS", 10.0),
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
        i2c_scl_gpio_pin: int,
        battery_capacity_mah: int,
        min_discharge_current_ma_for_runtime: int,
    ) -> None:
        self._i2c_addresses = i2c_addresses
        self._shunt_ohms = shunt_ohms
        self._max_expected_amps = max_expected_amps
        self._gain_strategy = gain_strategy
        self._bus_voltage_range_v = bus_voltage_range_v
        self._i2c_bus = i2c_bus
        self._i2c_scl_gpio_pin = i2c_scl_gpio_pin
        self._battery_capacity_mah = battery_capacity_mah
        self._min_discharge_current_ma_for_runtime = min_discharge_current_ma_for_runtime
        self._ina: AsyncINA219 | None = None

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

    async def reinitialize(self) -> bool:
        self._ina = None
        last_error: str | None = None
        candidate_list = ", ".join(f"0x{address:02X}" for address in self._i2c_addresses)
        print(
            "UPS monitor startup probe: "
            f"bus={self._i2c_bus} "
            f"addresses=[{candidate_list}]"
        )
        print(
            "UPS INA219 configuration: "
            f"regression_slope={1.0 / max(self._shunt_ohms, 1e-6)} "
            f"shunt_ohms={self._shunt_ohms} "
            f"gain_strategy={self._gain_strategy} "
            f"bus_voltage_range={self._bus_voltage_range_v}V "
            "bus_adc=128sample shunt_adc=128sample mode=triggered"
        )
        for address in self._i2c_addresses:
            try:
                self._ina = AsyncINA219(
                    address,
                    self._i2c_bus,
                    shunt_ohms=self._shunt_ohms,
                    bus_voltage_range_v=self._bus_voltage_range_v,
                    gain_strategy=self._gain_strategy,
                    scl_gpio_pin=self._i2c_scl_gpio_pin,
                )
                self._ina.set_calibration_profile(1.0 / max(self._shunt_ohms, 1e-6), 0.0)
                _, bus_raw, _ = await self._ina.trigger_and_fetch()
                bus_voltage_v = self._ina._raw_bus_to_voltage(bus_raw)
                self._ina.total_mAh = self._ina.estimate_starting_mah(
                    bus_voltage_v,
                    max_capacity_mah=float(self._battery_capacity_mah),
                )
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
        self._ina.set_calibration_profile(1.0 / max(self._shunt_ohms, 1e-6), 0.0)
        return True

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

    async def read(self) -> dict[str, Any]:
        read_started = asyncio.get_running_loop().time()

        def _finalize(payload: dict[str, Any]) -> dict[str, Any]:
            payload["read_latency_ms"] = round((asyncio.get_running_loop().time() - read_started) * 1000, 2)
            payload["read_status"] = str(payload.get("status", "unknown"))
            return payload

        if not self._ina:
            return _finalize({"status": "offline"})

        try:
            metrics = await self._ina.read()
            if bool(metrics.get("ovf")):
                metrics["overflow_diagnostic"] = self.build_overflow_diagnostic()
            current_ma = float(metrics["current_ma"])
            state_of_charge_pct_estimate = self._estimate_soc(float(metrics["bus_voltage_v"]))
            estimated_runtime_hours: float | None = None
            discharge_current_ma = abs(current_ma)
            if current_ma < -self._min_discharge_current_ma_for_runtime:
                remaining_capacity_mah = (state_of_charge_pct_estimate / 100.0) * float(self._battery_capacity_mah)
                estimated_runtime_hours = round(remaining_capacity_mah / discharge_current_ma, 2)
            external_input_present = float(metrics["bus_voltage_v"]) >= EXTERNAL_INPUT_PRESENT_MIN_BUS_V
            charging = external_input_present and current_ma > CURRENT_FLOW_DEADBAND_MA
            return _finalize({
                "status": "ok",
                **metrics,
                "state_of_charge_pct_estimate": state_of_charge_pct_estimate,
                "battery_capacity_mah": self._battery_capacity_mah,
                "estimated_runtime_hours": estimated_runtime_hours,
                "estimate_method": self.SOC_ESTIMATE_METHOD,
                # Compatibility field for legacy consumers.
                # Gate on external input + current deadband to avoid false positives
                # when the Pi is unplugged but sensor noise is slightly positive.
                "is_charging": charging,
            })
        except Exception as exc:
            return _finalize({"status": "read_error", "message": str(exc)})




async def configure_sqlite(conn: aiosqlite.Connection) -> None:
    """Apply low-overhead SQLite settings for SD-card backed storage."""
    await conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS};")
    journal_mode_cursor = await conn.execute("PRAGMA journal_mode=WAL;")
    journal_mode_row = await journal_mode_cursor.fetchone()
    await conn.execute("PRAGMA synchronous=NORMAL;")
    await conn.execute("PRAGMA temp_store=MEMORY;")
    if journal_mode_row and str(journal_mode_row[0]).lower() != "wal":
        logger.warning("SQLite journal mode is %s (expected WAL)", journal_mode_row[0])

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
    shutdown_requested: bool = False


def _evaluate_shutdown_trip(payload: dict[str, Any], config: Config) -> dict[str, Any] | None:
    status = str(payload.get("read_status", payload.get("status", "unknown")))
    if status != "ok":
        return None

    soc = float(payload.get("state_of_charge_pct_estimate", 0.0))
    voltage = float(payload.get("bus_voltage_v", 0.0))
    power_state = str(payload.get("power_state", "unknown"))
    current_ma = float(payload.get("current_ma", 0.0))

    soc_breach = soc <= config.shutdown_soc_trip_pct
    voltage_breach = voltage <= config.shutdown_voltage_trip_v
    discharging_direction = power_state in {"discharging", "brownout_risk", "battery_only"} or current_ma < -20.0
    charging_direction = power_state == "charging" or current_ma > 20.0

    if (soc_breach or voltage_breach) and discharging_direction and not charging_direction:
        reasons: list[str] = []
        if soc_breach:
            reasons.append("soc_below_threshold")
        if voltage_breach:
            reasons.append("voltage_below_threshold")
        return {
            "reasons": reasons,
            "soc_pct": soc,
            "voltage_v": voltage,
            "power_state": power_state,
            "current_ma": current_ma,
            "soc_threshold_pct": config.shutdown_soc_trip_pct,
            "voltage_threshold_v": config.shutdown_voltage_trip_v,
            "discharging_direction": discharging_direction,
        }
    return None


def _evaluate_shutdown_recovery(payload: dict[str, Any], config: Config) -> bool:
    status = str(payload.get("read_status", payload.get("status", "unknown")))
    if status != "ok":
        return False

    soc = float(payload.get("state_of_charge_pct_estimate", 0.0))
    voltage = float(payload.get("bus_voltage_v", 0.0))
    return soc >= config.shutdown_soc_recover_pct and voltage >= config.shutdown_voltage_recover_v


async def _request_host_poweroff(
    *,
    timeout_seconds: float,
    command: tuple[str, ...] = ("systemctl", "poweroff"),
) -> tuple[bool, str]:
    command_str = " ".join(shlex.quote(part) for part in command)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, f"poweroff_command_not_found:{command_str}"
    except Exception as exc:
        return False, f"poweroff_spawn_failed:{type(exc).__name__}:{exc}"

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()
        return False, f"poweroff_timeout:{timeout_seconds}s command={command_str}"
    except Exception as exc:
        process.kill()
        await process.wait()
        return False, f"poweroff_exec_failed:{type(exc).__name__}:{exc}"

    stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
    output = " | ".join(part for part in (stdout_text, stderr_text) if part)

    if process.returncode == 0:
        return True, output or "poweroff_command_submitted"
    return False, f"poweroff_exit_{process.returncode}:{output or 'no_output'}"


def _emit_shutdown_log(
    event: str,
    *,
    sample_timestamp: str,
    voltage_v: float,
    soc_pct_estimate: float,
    debounce_count: int,
    command_result: dict[str, Any] | None = None,
) -> None:
    structured_log: dict[str, Any] = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sample_timestamp": sample_timestamp,
        "voltage_v": round(float(voltage_v), 3),
        "soc_pct_estimate": round(float(soc_pct_estimate), 2),
        "debounce_count": int(debounce_count),
    }
    if command_result is not None:
        structured_log["command_result"] = command_result
    print(json.dumps(structured_log, separators=(",", ":"), sort_keys=True))


def _derive_power_flags(payload: dict[str, Any]) -> dict[str, bool]:
    status = payload.get("read_status", payload.get("status"))
    sensor_fault = status != "ok"
    overflow_fault = bool(payload.get("ovf"))
    current_ma = float(payload.get("current_ma", 0.0))
    bus_voltage_v = float(payload.get("bus_voltage_v", 0.0))
    external_input_present = (not sensor_fault) and bus_voltage_v >= EXTERNAL_INPUT_PRESENT_MIN_BUS_V
    charging = (not sensor_fault) and external_input_present and current_ma > CURRENT_FLOW_DEADBAND_MA
    discharging = (not sensor_fault) and current_ma < -CURRENT_FLOW_DEADBAND_MA
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
        payload = await monitor.read()
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
            retry_payload = await monitor.read()
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
                    recalibrated_payload = await monitor.read()
                    recalibrated_status = str(recalibrated_payload.get("status", "unknown"))
                    if recalibrated_status == "ok":
                        payload = recalibrated_payload
                        status = recalibrated_status
                        recovery_steps.append("recalibration_success")
                if status != "ok":
                    stats.sensor_reinit_attempts += 1
                    recovery_steps.append("i2c_reinitialize")
                    if await monitor.reinitialize():
                        stats.sensor_reinit_successes += 1
                        stats.sensor_reinit_count += 1
                        reinit_payload = await monitor.read()
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
    low_battery_counter = 0
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

        shutdown_breach = _evaluate_shutdown_trip(payload, config)
        if shutdown_breach is not None:
            low_battery_counter += 1
            if (
                low_battery_counter >= config.shutdown_consecutive_samples
                and not stats.shutdown_requested
            ):
                stats.shutdown_requested = True
                shutdown_event_payload = {
                    "event": "shutdown_requested",
                    "reason": shutdown_breach["reasons"],
                    "trigger_sample_count": low_battery_counter,
                    "required_consecutive_samples": config.shutdown_consecutive_samples,
                    "thresholds": {
                        "soc_trip_pct": config.shutdown_soc_trip_pct,
                        "soc_recover_pct": config.shutdown_soc_recover_pct,
                        "bus_voltage_trip_v": config.shutdown_voltage_trip_v,
                        "bus_voltage_recover_v": config.shutdown_voltage_recover_v,
                    },
                    "measurement": {
                        "soc_pct": shutdown_breach["soc_pct"],
                        "bus_voltage_v": shutdown_breach["voltage_v"],
                        "current_ma": shutdown_breach["current_ma"],
                        "power_state": shutdown_breach["power_state"],
                    },
                    "debounce_started_at": occurred_at,
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                }
                await enqueue_event(
                    conn,
                    config.vehicle_id,
                    "power_event",
                    occurred_at,
                    shutdown_event_payload,
                    config.queue_max_events,
                )
                print(
                    "Power shutdown policy triggered: "
                    f"reasons={shutdown_breach['reasons']} "
                    f"samples={low_battery_counter}/{config.shutdown_consecutive_samples} "
                    f"soc={shutdown_breach['soc_pct']} "
                    f"voltage={shutdown_breach['voltage_v']}"
                )
                _emit_shutdown_log(
                    "low_battery_detected",
                    sample_timestamp=occurred_at,
                    voltage_v=float(shutdown_breach["voltage_v"]),
                    soc_pct_estimate=float(shutdown_breach["soc_pct"]),
                    debounce_count=low_battery_counter,
                )
                _emit_shutdown_log(
                    "shutdown_requested",
                    sample_timestamp=occurred_at,
                    voltage_v=float(shutdown_breach["voltage_v"]),
                    soc_pct_estimate=float(shutdown_breach["soc_pct"]),
                    debounce_count=low_battery_counter,
                )
                shutdown_ok, shutdown_detail = await _request_host_poweroff(
                    timeout_seconds=config.shutdown_command_timeout_seconds
                )
                _emit_shutdown_log(
                    "shutdown_command_result",
                    sample_timestamp=occurred_at,
                    voltage_v=float(shutdown_breach["voltage_v"]),
                    soc_pct_estimate=float(shutdown_breach["soc_pct"]),
                    debounce_count=low_battery_counter,
                    command_result={"ok": shutdown_ok, "detail": shutdown_detail},
                )
                if shutdown_ok:
                    print(f"Host poweroff request accepted: {shutdown_detail}")
                else:
                    print(f"Host poweroff request failed: {shutdown_detail}")
        elif _evaluate_shutdown_recovery(payload, config):
            if low_battery_counter > 0:
                print(
                    "Power shutdown debounce reset after recovery: "
                    f"counter={low_battery_counter} "
                    f"soc={payload.get('state_of_charge_pct_estimate')} "
                    f"voltage={payload.get('bus_voltage_v')}"
                )
            low_battery_counter = 0

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
    print(
        "Shutdown policy settings: "
        f"soc_trip_pct={config.shutdown_soc_trip_pct} "
        f"soc_recover_pct={config.shutdown_soc_recover_pct} "
        f"voltage_trip_v={config.shutdown_voltage_trip_v} "
        f"voltage_recover_v={config.shutdown_voltage_recover_v} "
        f"consecutive_samples={config.shutdown_consecutive_samples} "
        f"command_timeout_s={config.shutdown_command_timeout_seconds}"
    )
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
        config.ina219_scl_gpio_pin,
        config.battery_capacity_mah,
        config.min_discharge_current_ma_for_runtime,
    )
    await monitor.reinitialize()
    stats = RuntimeStats()
    sensor_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max(1, config.queue_max_events // 2))

    async with aiosqlite.connect(
        config.db_path,
        timeout=SQLITE_CONNECT_TIMEOUT_SECONDS,
        isolation_level="IMMEDIATE",
    ) as conn:
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
