import asyncio
import json
import logging
import os
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import uvloop
from ina219 import INA219, DeviceRangeError

sys.path.append(str(Path(__file__).resolve().parents[1]))

from shared.hardware_probe import (
    build_hardware_inventory,
    parse_bool_env,
    parse_hex_list_env,
    parse_int_env,
    validate_inventory,
)

logger = logging.getLogger(__name__)

SQLITE_CONNECT_TIMEOUT_SECONDS = float(os.getenv("SQLITE_CONNECT_TIMEOUT_SECONDS", "30"))
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "30000"))
DEFAULT_UPS_MAX_EXPECTED_AMPS = 4.0
CURRENT_FLOW_DEADBAND_MA = 20.0
EXTERNAL_INPUT_PRESENT_MIN_BUS_V = 4.5


class INA219Driver:
    _VOLTAGE_SOC_MAX_V = 4.20
    _VOLTAGE_SOC_MIN_V = 3.20
    _GAIN_MAP = {
        "gain_1_40mv": INA219.GAIN_1_40MV,
        "gain_2_80mv": INA219.GAIN_2_80MV,
        "gain_4_160mv": INA219.GAIN_4_160MV,
        "gain_8_320mv": INA219.GAIN_8_320MV,
        "auto":         INA219.GAIN_AUTO,
    }

    def __init__(
        self,
        address: int,
        bus_num: int,
        *,
        shunt_ohms: float,
        max_expected_amps: float,
        bus_voltage_range_v: int,
        gain_strategy: str,
        q: float = 0.05,
        r: float = 100.0,
        p: float = 1.0,
        x: float = 0.0,
    ) -> None:
        self.address = address
        self._bus_num = bus_num
        self._shunt_ohms = shunt_ohms
        self._bus_voltage_range_v = bus_voltage_range_v
        self._gain_strategy = gain_strategy
        self._m = 1.0 / max(1e-6, shunt_ohms)
        self._c = 0.0
        self.Q = q
        self.R = r
        self.P = p
        self.K = 0.0
        self.X = x
        self.total_mAh = 0.0
        self._last_perf_counter = time.perf_counter()
        voltage_range = INA219.RANGE_16V if bus_voltage_range_v == 16 else INA219.RANGE_32V
        gain = self._GAIN_MAP.get(gain_strategy, INA219.GAIN_AUTO)
        self._sensor = INA219(
            shunt_ohms=shunt_ohms,
            max_expected_amps=max_expected_amps,
            address=address,
            busnum=bus_num,
        )
        self._sensor.configure(
            voltage_range=voltage_range,
            gain=gain,
            bus_adc=INA219.ADC_128SAMP,
            shunt_adc=INA219.ADC_128SAMP,
        )

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

    def estimate_starting_mah(self, current_voltage_v: float, max_capacity_mah: float = 1500.0) -> float:
        if current_voltage_v >= self._VOLTAGE_SOC_MAX_V:
            return max_capacity_mah
        if current_voltage_v <= self._VOLTAGE_SOC_MIN_V:
            return 0.0
        soc_pct = (current_voltage_v - self._VOLTAGE_SOC_MIN_V) / (
            self._VOLTAGE_SOC_MAX_V - self._VOLTAGE_SOC_MIN_V
        )
        return max_capacity_mah * soc_pct

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

    def _read_sync(self) -> dict[str, Any]:
        return {
            "bus_voltage_v":    self._sensor.voltage(),
            "shunt_voltage_mv": self._sensor.shunt_voltage(),
        }

    async def read(self) -> dict[str, Any]:
        raw = await asyncio.to_thread(self._read_sync)
        bus_voltage_v    = float(raw["bus_voltage_v"])
        shunt_voltage_mv = float(raw["shunt_voltage_mv"])
        shunt_voltage_v  = shunt_voltage_mv / 1000.0
        current_a        = (shunt_voltage_v * self._m) + self._c
        filtered_current_a = self._kalman_filter(current_a)
        delta_us = self._update_coulomb_counter(filtered_current_a)
        current_ma = filtered_current_a * 1000.0
        power_mw = bus_voltage_v * current_ma
        return {
            "bus_voltage_v":        round(bus_voltage_v, 3),
            "current_ma":           round(current_ma, 3),
            "power_mw":             round(power_mw, 3),
            "shunt_voltage_mv":     round(shunt_voltage_mv, 3),
            "cnvr":                 True,
            "ovf":                  False,
            "ina219_address":       f"0x{self.address:02X}",
            "total_mAh":            round(self.total_mAh, 6),
            "integration_delta_us": delta_us,
            "raw_current_a":        round(current_a, 6),
            "kalman_current_a":     round(filtered_current_a, 6),
        }


@dataclass(frozen=True)
class Config:
    vehicle_id: str
    db_path: str
    sample_interval_seconds: int
    ina219_addresses: tuple[int, ...]
    i2c_bus: int
    ina219_shunt_ohms: float
    ina219_max_expected_amps: float
    ina219_gain_strategy: str
    ina219_bus_voltage_range_v: int
    ina219_scl_gpio_pin: int
    battery_capacity_mah: int
    soc_voltage_min_v: float
    soc_voltage_full_resting_v: float
    soc_voltage_full_charging_v: float
    invert_current: bool
    min_discharge_current_ma_for_runtime: int
    shutdown_soc_trip_pct: float
    shutdown_soc_recover_pct: float
    shutdown_voltage_trip_v: float
    shutdown_voltage_recover_v: float
    shutdown_consecutive_samples: int
    shutdown_command_timeout_seconds: float
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
        soc_voltage_min_v=_read_float_env("UPS_SOC_VOLTAGE_MIN_V", 3.2),
        soc_voltage_full_resting_v=_read_float_env("UPS_SOC_VOLTAGE_FULL_RESTING_V", 4.05),
        soc_voltage_full_charging_v=_read_float_env("UPS_SOC_VOLTAGE_FULL_CHARGING_V", 4.25),
        invert_current=parse_bool_env("UPS_INVERT_CURRENT", False),
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
        queue_max_events=_read_int_env("POWER_QUEUE_MAX_EVENTS", 2000, minimum=100),
        maintenance_interval_seconds=_read_int_env("POWER_MAINTENANCE_INTERVAL_SECONDS", 30, minimum=10),
        imu_i2c_bus=parse_int_env("IMU_I2C_BUS", i2c_bus, minimum=0),
        imu_expected_addresses=parse_hex_list_env("IMU_EXPECTED_ADDRESSES", (0x6A,)),
        imu_required=False,  # power-monitor does not use the IMU; telematics-edge owns it
    )


class UpsMonitor:
    SOC_ESTIMATE_METHOD = "voltage_curve_loaded"
    COULOMB_SOC_SWITCHOVER_V = 4.25

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
        soc_voltage_min_v: float,
        soc_voltage_full_resting_v: float,
        soc_voltage_full_charging_v: float,
        min_discharge_current_ma_for_runtime: int,
        invert_current: bool = False,
    ) -> None:
        self._i2c_addresses = i2c_addresses
        self._shunt_ohms = shunt_ohms
        self._max_expected_amps = max_expected_amps
        self._gain_strategy = gain_strategy
        self._bus_voltage_range_v = bus_voltage_range_v
        self._i2c_bus = i2c_bus
        self._i2c_scl_gpio_pin = i2c_scl_gpio_pin
        self._battery_capacity_mah = battery_capacity_mah
        self._soc_voltage_min_v = soc_voltage_min_v
        self._soc_voltage_full_resting_v = max(soc_voltage_min_v + 0.01, soc_voltage_full_resting_v)
        self._soc_voltage_full_charging_v = max(
            self._soc_voltage_full_resting_v,
            soc_voltage_full_charging_v,
        )
        self._invert_current = invert_current
        self._min_discharge_current_ma_for_runtime = min_discharge_current_ma_for_runtime
        self._ina: INA219Driver | None = None

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
            f"regression_slope={(-1.0 if self._invert_current else 1.0) / max(self._shunt_ohms, 1e-6)} "
            f"shunt_ohms={self._shunt_ohms} "
            f"gain_strategy={self._gain_strategy} "
            f"bus_voltage_range={self._bus_voltage_range_v}V "
            "bus_adc=128sample shunt_adc=128sample mode=continuous"
        )
        for address in self._i2c_addresses:
            try:
                self._ina = INA219Driver(
                    address,
                    self._i2c_bus,
                    shunt_ohms=self._shunt_ohms,
                    max_expected_amps=self._max_expected_amps,
                    bus_voltage_range_v=self._bus_voltage_range_v,
                    gain_strategy=self._gain_strategy,
                )
                slope = (-1.0 if self._invert_current else 1.0) / max(self._shunt_ohms, 1e-6)
                self._ina.set_calibration_profile(slope, 0.0)
                bus_voltage_v = await asyncio.to_thread(self._ina._sensor.voltage)
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
        slope = (-1.0 if self._invert_current else 1.0) / max(self._shunt_ohms, 1e-6)
        self._ina.set_calibration_profile(slope, 0.0)
        return True

    def _estimate_soc(self, voltage_v: float, *, is_charging: bool) -> int:
        voltage_max_v = self._soc_voltage_full_charging_v if is_charging else self._soc_voltage_full_resting_v
        if voltage_v <= self._soc_voltage_min_v:
            return 0
        if voltage_v >= voltage_max_v:
            return 100
        curve_range_v = max(0.01, voltage_max_v - self._soc_voltage_min_v)
        ratio = (voltage_v - self._soc_voltage_min_v) / curve_range_v
        return int(round(ratio * 100.0))

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
            bus_voltage_v = float(metrics["bus_voltage_v"])
            total_mah = float(metrics.get("total_mAh", 0.0))
            external_input_present = bus_voltage_v >= EXTERNAL_INPUT_PRESENT_MIN_BUS_V
            charging = external_input_present and current_ma > CURRENT_FLOW_DEADBAND_MA
            charging_for_soc = current_ma > CURRENT_FLOW_DEADBAND_MA
            if bus_voltage_v > self.COULOMB_SOC_SWITCHOVER_V:
                state_of_charge_pct_estimate = int(round((total_mah / float(self._battery_capacity_mah)) * 100.0))
                estimate_method = "coulomb_counter_output_rail"
            else:
                state_of_charge_pct_estimate = self._estimate_soc(bus_voltage_v, is_charging=charging_for_soc)
                estimate_method = self.SOC_ESTIMATE_METHOD
            state_of_charge_pct_estimate = max(0, min(100, state_of_charge_pct_estimate))
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
                "estimate_method": estimate_method,
                # Compatibility field for legacy consumers.
                # Gate on external input + current deadband to avoid false positives
                # when the Pi is unplugged but sensor noise is slightly positive.
                "is_charging": charging,
            })
        except DeviceRangeError:
            return _finalize({
                "status": "range_error",
                "ovf": True,
                "overflow_diagnostic": self.build_overflow_diagnostic(),
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
        CREATE TABLE IF NOT EXISTS local_power (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            battery_percent REAL,
            power_state TEXT NOT NULL,
            occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await conn.commit()

async def insert_local_power(
    conn: aiosqlite.Connection,
    battery_percent: float | int | None,
    power_state: str,
) -> None:
    await conn.execute(
        "INSERT INTO local_power (battery_percent, power_state) VALUES (?, ?)",
        (battery_percent, power_state),
    )
    await conn.commit()


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
        battery_percent = payload.get("state_of_charge_pct_estimate")

        try:
            await insert_local_power(conn, battery_percent, current_state)
        except Exception as exc:
            logger.exception(
                "Failed to insert local_power row at %s (battery_percent=%s power_state=%s): %s",
                occurred_at,
                battery_percent,
                current_state,
                exc,
            )

        if previous_state is not None and current_state != previous_state:
            stats.state_transitions += 1
            print(f"Power state transition: {previous_state} -> {current_state} at {occurred_at}")

        shutdown_breach = _evaluate_shutdown_trip(payload, config)
        if shutdown_breach is not None:
            low_battery_counter += 1
            if (
                low_battery_counter >= config.shutdown_consecutive_samples
                and not stats.shutdown_requested
            ):
                stats.shutdown_requested = True
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

        previous_state = current_state
        in_queue.task_done()
        print(f"Stored power reading: {payload.get('status')} state={current_state} at {occurred_at}")


async def maintenance_loop(config: Config, conn: aiosqlite.Connection, stats: RuntimeStats) -> None:
    while True:
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
            f"shutdown_requested={stats.shutdown_requested}"
        )
        await asyncio.sleep(config.maintenance_interval_seconds)


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
        raise RuntimeError(
            "Hardware probe failed: "
            f"{'; '.join(inventory_errors)} "
            "Action: resolve the hardware and environment variable issues above, then restart power-monitor."
        )

    monitor = UpsMonitor(
        config.ina219_addresses,
        config.ina219_shunt_ohms,
        config.ina219_max_expected_amps,
        config.ina219_gain_strategy,
        config.ina219_bus_voltage_range_v,
        config.i2c_bus,
        config.ina219_scl_gpio_pin,
        config.battery_capacity_mah,
        config.soc_voltage_min_v,
        config.soc_voltage_full_resting_v,
        config.soc_voltage_full_charging_v,
        config.min_discharge_current_ma_for_runtime,
        invert_current=config.invert_current,
    )
    await monitor.reinitialize()
    stats = RuntimeStats()
    sensor_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max(1, config.queue_max_events // 2))

    async with aiosqlite.connect(
        config.db_path,
        timeout=SQLITE_CONNECT_TIMEOUT_SECONDS,
        # Autocommit mode: each write helper controls its own transaction boundary.
        # If we introduce batching later, use explicit BEGIN/COMMIT scopes only
        # around write-heavy sections.
        isolation_level=None,
    ) as conn:
        await configure_sqlite(conn)
        await init_db(conn)
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(sensor_loop(config, monitor, sensor_queue, stats))
            task_group.create_task(state_engine_loop(config, conn, sensor_queue, stats))
            task_group.create_task(maintenance_loop(config, conn, stats))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    uvloop.install()
    asyncio.run(run())
