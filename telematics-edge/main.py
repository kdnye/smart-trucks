import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
import pynmea2
import serial
from ina219 import INA219, DeviceRangeError

from db import init_db, insert_event, get_unsynced_events, mark_event_synced, get_db_stats, purge_old_synced_events
from imu_reader import IMUReader, ImuSnapshot, snapshot_as_dict


@dataclass(frozen=True)
class Config:
    vehicle_id: str
    webhook_url: str | None
    api_key: str
    sync_interval_seconds: int
    gps_serial_candidates: tuple[str, ...]
    gps_baud_rate: int


def load_config() -> Config:
    serial_candidates = tuple(
        candidate.strip()
        for candidate in os.getenv("GPS_SERIAL_CANDIDATES", "/dev/serial0,/dev/ttyAMA0,/dev/ttyS0").split(",")
        if candidate.strip()
    )
    primary_device = os.getenv("GPS_SERIAL_DEVICE", "/dev/serial0")
    serial_devices: tuple[str, ...] = tuple(dict.fromkeys((primary_device, *serial_candidates)))
    return Config(
        vehicle_id=os.getenv("VEHICLE_ID", "UNKNOWN_TRUCK"),
        webhook_url=os.getenv("WEBHOOK_URL"),
        api_key=os.getenv("API_KEY", ""),
        sync_interval_seconds=max(5, int(os.getenv("POLL_INTERVAL", "60"))),
        gps_serial_candidates=serial_devices,
        gps_baud_rate=max(1200, int(os.getenv("GPS_BAUD_RATE", "9600"))),
    )


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


def get_latest_gps(serial_devices: tuple[str, ...], baud_rate: int) -> dict[str, Any]:
    for serial_device in serial_devices:
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

                    return {
                        "fix_status": "locked",
                        "device": serial_device,
                        "latitude": latitude,
                        "longitude": longitude,
                        "altitude_m": getattr(msg, "altitude", None),
                        "speed_knots": getattr(msg, "spd_over_grnd", None),
                        "gps_timestamp": str(getattr(msg, "timestamp", "")) or None,
                    }
        except Exception as exc:
            print(f"GPS serial error on {serial_device}: {exc}")

    return {"fix_status": "searching"}


async def push_telemetry(session: aiohttp.ClientSession, config: Config, payload: dict[str, Any]) -> bool:
    """Push a payload to the cloud webhook. Returns True on success."""
    if not config.webhook_url:
        return False

    headers = {"X-Api-Key": config.api_key} if config.api_key else {}
    try:
        async with session.post(config.webhook_url, json=payload, headers=headers) as response:
            if response.status < 400:
                print(f"Telematics synced. Status={response.status}")
                return True
            else:
                body = await response.text()
                print(f"Sync failed. Status={response.status} Body={body[:200]}")
                return False
    except Exception as exc:
        print(f"Cloud connection failed: {exc}")
        return False


_MAINTENANCE_INTERVAL_SECONDS = 3600  # Run maintenance checks every hour


async def maintenance_worker() -> None:
    """Background task: log DB health stats and purge old synced events every hour."""
    while True:
        await asyncio.sleep(_MAINTENANCE_INTERVAL_SECONDS)
        stats = await get_db_stats()
        print(
            f"[Maintenance] DB health — pending={stats['pending']} synced={stats['synced']}"
        )
        deleted = await purge_old_synced_events(days=7)
        if deleted:
            print(f"[Maintenance] Purged {deleted} old synced events from database.")


async def run() -> None:
    config = load_config()
    power = PowerMonitor()
    imu = ImuMonitor()
    asyncio.create_task(imu.start())
    asyncio.create_task(maintenance_worker())

    await init_db()
    print(f"Starting telematics-edge for vehicle {config.vehicle_id}.")

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            # 1. Gather current sensor data
            payload = {
                "event_type": "edge_telematics_heartbeat",
                "vehicle_id": config.vehicle_id,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "uptime_seconds": int(time.monotonic()),
                "power_metrics": power.read(),
                "imu_metrics": imu.read(),
                "location": get_latest_gps(
                    config.gps_serial_candidates,
                    config.gps_baud_rate,
                ),
            }

            print(
                "GPS={gps} Power={power}V IMU={imu}".format(
                    gps=payload["location"].get("fix_status"),
                    power=payload["power_metrics"].get("voltage_v", "N/A"),
                    imu=payload["imu_metrics"].get("status"),
                )
            )

            # 2. Always persist locally first — data is never lost even if offline
            await insert_event(config.vehicle_id, "edge_telematics_heartbeat", payload)

            # 3. Drain the backlog (includes the event we just saved, oldest first)
            unsynced = await get_unsynced_events()
            for event_id, _event_type, saved_payload_str in unsynced:
                saved_payload = json.loads(saved_payload_str)
                success = await push_telemetry(session, config, saved_payload)
                if success:
                    await mark_event_synced(event_id)
                else:
                    # Stop retrying on the first failure — network is down.
                    # Remaining events stay queued and will be retried next cycle.
                    print("Network offline. Keeping data safely in database.")
                    break

            await asyncio.sleep(config.sync_interval_seconds)


if __name__ == "__main__":
    asyncio.run(run())
