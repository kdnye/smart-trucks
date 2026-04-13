import asyncio
import logging
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from smbus2 import SMBus

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


class IMUReader:
    """LSM6DSL reader with 10Hz sampling and 1Hz snapshot emission."""

    def __init__(self, bus_num: int = 1) -> None:
        self.bus_num = bus_num
        self.bus: SMBus | None = None

        self.harsh_threshold_g = float(os.getenv("HARSH_EVENT_G_THRESHOLD", "0.4"))
        # LSM6DSL sensitivity at +/-2g scale.
        self.accel_sensitivity_g = 0.061 / 1000.0

    def connect(self) -> bool:
        try:
            self.bus = SMBus(self.bus_num)
            whoami = self.bus.read_byte_data(LSM6DSL_ADDRESS, WHO_AM_I)
            if whoami != 0x6A:
                logger.warning("Unexpected LSM6DSL WHO_AM_I value: 0x%02X", whoami)

            # 208Hz ODR, +/-2g scale, anti-aliasing 100Hz.
            self.bus.write_byte_data(LSM6DSL_ADDRESS, CTRL1_XL, 0x50)
            logger.info(
                "IMU initialized on I2C 0x%02X (WHO_AM_I=0x%02X). Threshold=%0.2fg",
                LSM6DSL_ADDRESS,
                whoami,
                self.harsh_threshold_g,
            )
            return True
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Could not initialize IMU on bus %s: %s", self.bus_num, exc)
            self._close_bus()
            return False

    def _close_bus(self) -> None:
        if self.bus:
            try:
                self.bus.close()
            except Exception:  # pylint: disable=broad-except
                pass
        self.bus = None

    def _read_word_2c(self, register: int) -> int:
        if not self.bus:
            return 0
        low = self.bus.read_byte_data(LSM6DSL_ADDRESS, register)
        high = self.bus.read_byte_data(LSM6DSL_ADDRESS, register + 1)
        value = (high << 8) | low
        return value - 65536 if value >= 32768 else value

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

        while True:
            if not self.bus and not self.connect():
                await asyncio.sleep(5)
                continue

            try:
                accel_x, accel_y, accel_z = await asyncio.to_thread(self.get_acceleration)
                magnitude_2d = math.sqrt(accel_x**2 + accel_y**2)
                is_harsh = magnitude_2d >= self.harsh_threshold_g

                snapshot = ImuSnapshot(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    accel_x=round(accel_x, 4),
                    accel_y=round(accel_y, 4),
                    accel_z=round(accel_z, 4),
                    magnitude_2d=round(magnitude_2d, 4),
                    is_harsh_event=is_harsh,
                )

                if is_harsh:
                    logger.warning("HARSH EVENT DETECTED! Force=%0.2fg", magnitude_2d)
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
                self._close_bus()
                await asyncio.sleep(1)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("IMU read error: %s", exc)
                await asyncio.sleep(1)

            await asyncio.sleep(sample_interval_seconds)


def snapshot_as_dict(snapshot: ImuSnapshot) -> dict[str, Any]:
    return asdict(snapshot)
