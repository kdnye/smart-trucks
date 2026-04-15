import asyncio
import logging
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from smbus2 import SMBus, i2c_msg

logger = logging.getLogger(__name__)

# LSM6DSL I2C Address + registers
LSM6DSL_ADDRESS = 0x6A
WHO_AM_I = 0x0F
CTRL1_XL = 0x10
OUTX_L_XL = 0x28


@dataclass
class IMUSnapshot:
    timestamp: str
    accel_x: float
    accel_y: float
    accel_z: float
    magnitude_2d: float
    is_harsh_event: bool


# Backward compatibility for existing imports.
ImuSnapshot = IMUSnapshot


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


class IMUReader:
    """LSM6DSL reader with 10Hz sampling and 1Hz snapshot emission."""

    def __init__(self, bus_num: int = 1) -> None:
        self.bus_num = bus_num

        self.harsh_threshold_g = float(os.getenv("HARSH_EVENT_G_THRESHOLD", "0.4"))
        self.gravity_baseline_g = float(os.getenv("GRAVITY_BASELINE_G", "1.0"))
        # LSM6DSL sensitivity at +/-2g scale.
        self.accel_sensitivity_g = 0.061 / 1000.0

    def connect(self) -> bool:
        try:
            whoami = self._read_register_byte(WHO_AM_I)
            if whoami != 0x6A:
                logger.warning("Unexpected LSM6DSL WHO_AM_I value: 0x%02X", whoami)

            # 208Hz ODR, +/-2g scale, anti-aliasing 100Hz.
            with SMBus(self.bus_num) as bus:
                bus.write_byte_data(LSM6DSL_ADDRESS, CTRL1_XL, 0x50)
            logger.info(
                "IMU initialized on I2C 0x%02X (WHO_AM_I=0x%02X). Threshold=%0.2fg, Gravity baseline=%0.2fg",
                LSM6DSL_ADDRESS,
                whoami,
                self.harsh_threshold_g,
                self.gravity_baseline_g,
            )
            return True
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Could not initialize IMU on bus %s: %s", self.bus_num, exc)
            return False

    def _read_word_2c(self, register: int) -> int:
        raw = read_i2c_atomic(self.bus_num, LSM6DSL_ADDRESS, register, 2)
        low = raw[0]
        high = raw[1]
        value = (high << 8) | low
        return value - 65536 if value >= 32768 else value

    def _read_register_byte(self, register: int) -> int:
        return read_i2c_atomic(self.bus_num, LSM6DSL_ADDRESS, register, 1)[0]

    def get_acceleration(self) -> tuple[float, float, float]:
        x = self._read_word_2c(OUTX_L_XL) * self.accel_sensitivity_g
        y = self._read_word_2c(OUTX_L_XL + 2) * self.accel_sensitivity_g
        z = self._read_word_2c(OUTX_L_XL + 4) * self.accel_sensitivity_g
        return x, y, z

    async def read_loop(
        self,
        snapshot_callback: Callable[[ImuSnapshot], Awaitable[None]],
        harsh_event_callback: Callable[[ImuSnapshot], Awaitable[None]],
        sample_interval_seconds: float = 0.1,
    ) -> None:
        snapshot_interval_seconds = 1.0
        debounce_seconds = 2.0
        last_snapshot_time = 0.0

        # Connect once at startup; only reconnect after an I2C error.
        # Previously connect() was called on every loop iteration (every 0.1 s),
        # which spammed "IMU initialized" at ~10 Hz. Now it is called once here
        # and again only in the OSError recovery path.
        while not self.connect():
            await asyncio.sleep(5)

        while True:
            try:
                accel_x, accel_y, accel_z = await asyncio.to_thread(self.get_acceleration)
                magnitude_2d = math.sqrt(accel_x**2 + accel_y**2)
                magnitude_3d = math.sqrt(accel_x**2 + accel_y**2 + accel_z**2)
                dynamic_force_g = max(0.0, magnitude_3d - self.gravity_baseline_g)
                is_harsh = dynamic_force_g >= self.harsh_threshold_g

                snapshot = ImuSnapshot(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    accel_x=round(accel_x, 4),
                    accel_y=round(accel_y, 4),
                    accel_z=round(accel_z, 4),
                    magnitude_2d=round(magnitude_2d, 4),
                    is_harsh_event=is_harsh,
                )

                if is_harsh:
                    logger.warning(
                        "HARSH EVENT DETECTED! Dynamic=%0.2fg (total=%0.2fg, baseline=%0.2fg)",
                        dynamic_force_g,
                        magnitude_3d,
                        self.gravity_baseline_g,
                    )
                    await harsh_event_callback(snapshot)
                    await asyncio.sleep(debounce_seconds)
                    last_snapshot_time = asyncio.get_running_loop().time()
                    continue

                current_time = asyncio.get_running_loop().time()
                if current_time - last_snapshot_time >= snapshot_interval_seconds:
                    await snapshot_callback(snapshot)
                    last_snapshot_time = current_time
            except OSError as exc:
                logger.error("IMU I2C read error: %s. Resetting bus.", exc)
                await asyncio.sleep(1)
                while not self.connect():
                    await asyncio.sleep(5)
                continue
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("IMU read error: %s", exc)
                await asyncio.sleep(1)

            await asyncio.sleep(sample_interval_seconds)


def snapshot_as_dict(snapshot: ImuSnapshot) -> dict[str, Any]:
    return asdict(snapshot)
