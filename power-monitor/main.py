import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
import aiosqlite
from ina219 import INA219, DeviceRangeError


@dataclass(frozen=True)
class Config:
    vehicle_id: str
    db_path: str
    webhook_url: str | None
    api_key: str
    sample_interval_seconds: int
    ina219_address: int
    ina219_shunt_ohms: float


def _read_int_env(name: str, default: int, *, minimum: int | None = None) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        parsed = default
    else:
        value = raw_value.strip()
        if not value or (value.startswith("${") and value.endswith("}")):
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


def load_config() -> Config:
    return Config(
        vehicle_id=os.getenv("VEHICLE_ID", "UNKNOWN_TRUCK"),
        db_path=os.getenv("TELEMATICS_DB_PATH", "/data/telematics.db"),
        webhook_url=os.getenv("WEBHOOK_URL"),
        api_key=os.getenv("API_KEY", ""),
        sample_interval_seconds=_read_int_env("POWER_SAMPLE_INTERVAL_SECONDS", 10, minimum=5),
        ina219_address=int(os.getenv("UPS_I2C_ADDRESS", "0x43"), 16),
        ina219_shunt_ohms=float(os.getenv("UPS_SHUNT_OHMS", "0.1")),
    )


class UpsMonitor:
    def __init__(self, i2c_address: int, shunt_ohms: float) -> None:
        self._ina: INA219 | None = None
        try:
            self._ina = INA219(shunt_ohms=shunt_ohms, address=i2c_address)
            self._ina.configure()
            print(f"UPS monitor initialized at I2C address 0x{i2c_address:02X}.")
        except Exception as exc:
            print(f"Warning: UPS monitor unavailable on 0x{i2c_address:02X}: {exc}")

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
        if not self._ina:
            return {"status": "offline"}

        try:
            voltage = round(self._ina.voltage(), 3)
            current = round(self._ina.current(), 3)
            power = round(self._ina.power(), 3)
            return {
                "status": "ok",
                "bus_voltage_v": voltage,
                "current_ma": current,
                "power_mw": power,
                "state_of_charge_pct": self._estimate_soc(voltage),
                "is_charging": current > 0,
            }
        except DeviceRangeError:
            return {"status": "range_error"}
        except Exception as exc:
            return {"status": "read_error", "message": str(exc)}


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
            synced INTEGER DEFAULT 0
        )
        """
    )
    await conn.commit()


async def write_reading(conn: aiosqlite.Connection, vehicle_id: str, occurred_at: str, payload: dict[str, Any]) -> None:
    json_payload = json.dumps(payload, separators=(",", ":"))
    await conn.execute(
        "INSERT INTO power_readings(vehicle_id, occurred_at, payload) VALUES(?, ?, ?)",
        (vehicle_id, occurred_at, json_payload),
    )
    await conn.execute(
        "INSERT INTO events(vehicle_id, event_type, occurred_at, payload, synced) VALUES(?, 'power_snapshot', ?, ?, 0)",
        (vehicle_id, occurred_at, json_payload),
    )
    await conn.commit()


async def publish_snapshot(
    session: aiohttp.ClientSession,
    webhook_url: str | None,
    api_key: str,
    vehicle_id: str,
    occurred_at: str,
    payload: dict[str, Any],
) -> None:
    if not webhook_url:
        return

    body = {
        "event_type": "power_snapshot",
        "vehicle_id": vehicle_id,
        "occurred_at": occurred_at,
        "power_metrics": payload,
    }
    headers = {"X-Api-Key": api_key} if api_key else {}
    try:
        async with session.post(webhook_url, json=body, headers=headers) as response:
            if response.status >= 400:
                print(f"Power snapshot upload failed with status={response.status}")
    except Exception as exc:
        print(f"Power snapshot upload error: {exc}")


async def run() -> None:
    config = load_config()
    monitor = UpsMonitor(config.ina219_address, config.ina219_shunt_ohms)

    async with aiosqlite.connect(config.db_path) as conn:
        await init_db(conn)
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                occurred_at = datetime.now(timezone.utc).isoformat()
                payload = monitor.read()
                await write_reading(conn, config.vehicle_id, occurred_at, payload)
                await publish_snapshot(
                    session,
                    config.webhook_url,
                    config.api_key,
                    config.vehicle_id,
                    occurred_at,
                    payload,
                )
                print(f"Stored power reading: {payload.get('status')} at {occurred_at}")
                await asyncio.sleep(config.sample_interval_seconds)


if __name__ == "__main__":
    asyncio.run(run())
