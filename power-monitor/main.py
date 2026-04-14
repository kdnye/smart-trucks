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
    ina219_addresses: tuple[int, ...]
    ina219_shunt_ohms: float


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

    return Config(
        vehicle_id=os.getenv("VEHICLE_ID", "UNKNOWN_TRUCK"),
        db_path=os.getenv("TELEMATICS_DB_PATH", "/data/telematics.db"),
        webhook_url=os.getenv("WEBHOOK_URL"),
        api_key=os.getenv("API_KEY", ""),
        sample_interval_seconds=_read_int_env("POWER_SAMPLE_INTERVAL_SECONDS", 10, minimum=5),
        ina219_addresses=ina219_addresses,
        ina219_shunt_ohms=_read_float_env("UPS_SHUNT_OHMS", 0.1),
    )


class UpsMonitor:
    def __init__(self, i2c_addresses: tuple[int, ...], shunt_ohms: float) -> None:
        self._ina: INA219 | None = None
        last_error: str | None = None
        for address in i2c_addresses:
            try:
                self._ina = INA219(shunt_ohms=shunt_ohms, address=address)
                self._ina.configure()
                print(f"UPS monitor initialized at I2C address 0x{address:02X}.")
                return
            except Exception as exc:
                last_error = str(exc)
                print(f"Warning: UPS monitor unavailable on 0x{address:02X}: {exc}")

        if last_error:
            candidate_list = ", ".join(f"0x{address:02X}" for address in i2c_addresses)
            print(f"Warning: UPS monitor unavailable on all candidate I2C addresses ({candidate_list}).")

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


async def write_reading(
    conn: aiosqlite.Connection,
    vehicle_id: str,
    occurred_at: str,
    payload: dict[str, Any],
) -> int:
    """Persist a power reading and return the events table row id."""
    json_payload = json.dumps(payload, separators=(",", ":"))
    await conn.execute(
        "INSERT INTO power_readings(vehicle_id, occurred_at, payload) VALUES(?, ?, ?)",
        (vehicle_id, occurred_at, json_payload),
    )
    cursor = await conn.execute(
        "INSERT INTO events(vehicle_id, event_type, occurred_at, payload, synced) VALUES(?, 'power_snapshot', ?, ?, 0)",
        (vehicle_id, occurred_at, json_payload),
    )
    await conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def publish_snapshot(
    session: aiohttp.ClientSession,
    webhook_url: str | None,
    api_key: str,
    vehicle_id: str,
    occurred_at: str,
    payload: dict[str, Any],
) -> bool:
    """Upload a power snapshot, retrying up to 3 times with exponential backoff."""
    if not webhook_url:
        return False

    body = {
        "event_type": "power_snapshot",
        "vehicle_id": vehicle_id,
        "occurred_at": occurred_at,
        "power_metrics": payload,
    }
    headers = {"X-Api-Key": api_key} if api_key else {}

    backoff = 2
    for attempt in range(4):
        try:
            async with session.post(webhook_url, json=body, headers=headers) as response:
                if response.status < 400:
                    return True
                error_body = await response.text()
                print(
                    f"Power snapshot upload failed: status={response.status} "
                    f"body={error_body[:200]!r} (attempt {attempt + 1})"
                )
                if response.status < 500:
                    # 4xx client error — retrying won't help.
                    return False
        except Exception as exc:
            print(f"Power snapshot upload error (attempt {attempt + 1}): {exc}")

        if attempt < 3:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    return False


async def drain_unsynced_worker(
    config: Config,
    conn: aiosqlite.Connection,
    session: aiohttp.ClientSession,
) -> None:
    """Periodically drain events buffered offline (synced=0), oldest-first."""
    while True:
        await asyncio.sleep(60)
        if not config.webhook_url:
            continue

        async with conn.execute(
            "SELECT id, vehicle_id, occurred_at, payload FROM events WHERE synced=0 ORDER BY id LIMIT 50"
        ) as cursor:
            rows = await cursor.fetchall()

        for row_id, vehicle_id, occurred_at, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except (json.JSONDecodeError, ValueError):
                continue

            uploaded = await publish_snapshot(
                session,
                config.webhook_url,
                config.api_key,
                vehicle_id,
                occurred_at,
                payload,
            )
            if uploaded:
                await conn.execute("UPDATE events SET synced=1 WHERE id=?", (row_id,))
                await conn.commit()
            else:
                # Stop draining on persistent failure; retry next cycle.
                break


async def run() -> None:
    config = load_config()
    monitor = UpsMonitor(config.ina219_addresses, config.ina219_shunt_ohms)

    async with aiosqlite.connect(config.db_path) as conn:
        await init_db(conn)
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            asyncio.create_task(drain_unsynced_worker(config, conn, session))
            while True:
                occurred_at = datetime.now(timezone.utc).isoformat()
                payload = monitor.read()
                event_id = await write_reading(conn, config.vehicle_id, occurred_at, payload)
                uploaded = await publish_snapshot(
                    session,
                    config.webhook_url,
                    config.api_key,
                    config.vehicle_id,
                    occurred_at,
                    payload,
                )
                if uploaded:
                    await conn.execute("UPDATE events SET synced=1 WHERE id=?", (event_id,))
                    await conn.commit()
                print(f"Stored power reading: {payload.get('status')} at {occurred_at}")
                await asyncio.sleep(config.sample_interval_seconds)


if __name__ == "__main__":
    asyncio.run(run())
