import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
import pynmea2
import serial
from ina219 import INA219, DeviceRangeError
from smbus2 import SMBus


@dataclass(frozen=True)
class Config:
    vehicle_id: str
    webhook_url: str | None
    api_key: str
    sync_interval_seconds: int
    gps_serial_device: str
    gps_baud_rate: int


def load_config() -> Config:
    return Config(
        vehicle_id=os.getenv("VEHICLE_ID", "UNKNOWN_TRUCK"),
        webhook_url=os.getenv("WEBHOOK_URL"),
        api_key=os.getenv("API_KEY", ""),
        sync_interval_seconds=max(5, int(os.getenv("POLL_INTERVAL", "60"))),
        gps_serial_device=os.getenv("GPS_SERIAL_DEVICE", "/dev/ttyS0"),
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
    """Minimal Berry IMU probe/read on I2C bus 1.

    This attempts to read the accelerometer from common BerryIMU v4 layouts where the
    accelerometer/gyro is exposed via LSM6DSL-compatible registers on address 0x6A.
    If unavailable, telemetry reports IMU as offline without crashing the service.
    """

    _ADDR = 0x6A
    _WHO_AM_I = 0x0F
    _CTRL1_XL = 0x10
    _OUTX_L_XL = 0x28

    def __init__(self, bus_id: int = 1) -> None:
        self._bus_id = bus_id
        self._active = False
        try:
            with SMBus(self._bus_id) as bus:
                whoami = bus.read_byte_data(self._ADDR, self._WHO_AM_I)
                # Accept common ST responses seen on this register family.
                if whoami in {0x6A, 0x6C}:
                    # 104 Hz, ±2g
                    bus.write_byte_data(self._ADDR, self._CTRL1_XL, 0x40)
                    self._active = True
                    print(f"IMU initialized on I2C 0x{self._ADDR:02X} (WHO_AM_I=0x{whoami:02X}).")
                else:
                    print(f"Warning: unexpected IMU WHO_AM_I value 0x{whoami:02X}; IMU disabled.")
        except Exception as exc:
            print(f"Warning: IMU init failed on I2C bus {self._bus_id}: {exc}")

    def read(self) -> dict[str, Any]:
        if not self._active:
            return {"status": "offline"}
        try:
            with SMBus(self._bus_id) as bus:
                data = bus.read_i2c_block_data(self._ADDR, self._OUTX_L_XL, 6)

            def to_int16(lo: int, hi: int) -> int:
                value = (hi << 8) | lo
                return value - 65536 if value > 32767 else value

            # ±2g => 0.061 mg/LSB per datasheet
            scale_g_per_lsb = 0.000061
            ax = round(to_int16(data[0], data[1]) * scale_g_per_lsb, 4)
            ay = round(to_int16(data[2], data[3]) * scale_g_per_lsb, 4)
            az = round(to_int16(data[4], data[5]) * scale_g_per_lsb, 4)
            return {"status": "ok", "accel_g": {"x": ax, "y": ay, "z": az}}
        except Exception as exc:
            return {"status": "read_error", "message": str(exc)}


def get_latest_gps(serial_device: str, baud_rate: int) -> dict[str, Any]:
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
                    "latitude": latitude,
                    "longitude": longitude,
                    "altitude_m": getattr(msg, "altitude", None),
                    "speed_knots": getattr(msg, "spd_over_grnd", None),
                    "gps_timestamp": str(getattr(msg, "timestamp", "")) or None,
                }
    except Exception as exc:
        print(f"GPS serial error on {serial_device}: {exc}")

    return {"fix_status": "searching"}


async def push_telemetry(session: aiohttp.ClientSession, config: Config, payload: dict[str, Any]) -> None:
    if not config.webhook_url:
        return

    headers = {"X-Api-Key": config.api_key} if config.api_key else {}
    try:
        async with session.post(config.webhook_url, json=payload, headers=headers) as response:
            if response.status < 400:
                print(f"Telematics synced. Status={response.status}")
            else:
                body = await response.text()
                print(f"Sync failed. Status={response.status} Body={body[:200]}")
    except Exception as exc:
        print(f"Cloud connection failed: {exc}")


async def run() -> None:
    config = load_config()
    power = PowerMonitor()
    imu = ImuMonitor()

    print(f"Starting telematics-edge for vehicle {config.vehicle_id}.")

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            payload = {
                "event_type": "edge_telematics_heartbeat",
                "vehicle_id": config.vehicle_id,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "uptime_seconds": int(time.monotonic()),
                "power_metrics": power.read(),
                "imu_metrics": imu.read(),
                "location": get_latest_gps(config.gps_serial_device, config.gps_baud_rate),
            }

            print(
                "GPS={gps} Power={power}V IMU={imu}".format(
                    gps=payload["location"].get("fix_status"),
                    power=payload["power_metrics"].get("voltage_v", "N/A"),
                    imu=payload["imu_metrics"].get("status"),
                )
            )
            await push_telemetry(session, config, payload)
            await asyncio.sleep(config.sync_interval_seconds)


if __name__ == "__main__":
    asyncio.run(run())
