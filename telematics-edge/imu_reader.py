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

WHO_AM_I = 0x0F

# LSM6DSL — 6-DOF accel/gyro at 0x6A (existing hardware)
LSM6DSL_ADDRESS = 0x6A
LSM6DSL_WHO_AM_I_VALUE = 0x6A
LSM6DSL_CTRL1_XL = 0x10   # accel: 208 Hz, ±2 g
LSM6DSL_CTRL2_G = 0x11    # gyro:  208 Hz, 245 dps
LSM6DSL_OUTX_L_G = 0x22   # gyro data start
LSM6DSL_OUTX_L_XL = 0x28  # accel data start

# LSM9DS1 — 9-DOF accel/gyro at 0x6B + mag at 0x1E (BerryGPS-IMU V4)
LSM9DS1_AG_ADDRESS = 0x6B
LSM9DS1_AG_WHO_AM_I_VALUE = 0x68
LSM9DS1_CTRL_REG1_G = 0x10   # gyro:  119 Hz, 245 dps
LSM9DS1_CTRL_REG6_XL = 0x20  # accel: 119 Hz, ±2 g
LSM9DS1_OUT_X_L_G = 0x18     # gyro data start
LSM9DS1_OUT_X_L_XL = 0x28    # accel data start

LSM9DS1_MAG_ADDRESS = 0x1E
LSM9DS1_MAG_WHO_AM_I_VALUE = 0x3D
LSM9DS1_CTRL_REG1_M = 0x20  # temp-comp, ultra perf, 80 Hz
LSM9DS1_CTRL_REG2_M = 0x21  # ±4 G range
LSM9DS1_CTRL_REG3_M = 0x22  # continuous-conversion mode
LSM9DS1_CTRL_REG4_M = 0x23  # z-axis ultra performance
LSM9DS1_OUT_X_L_M = 0x28    # mag data start
LSM9DS1_MAG_SENSITIVITY = 0.14  # µT / LSB at ±4 G

# LPS25H — barometer at 0x5D (BerryGPS-IMU V4)
LPS25H_ADDRESS = 0x5D
LPS25H_WHO_AM_I_VALUE = 0xBD
LPS25H_CTRL_REG1 = 0x20    # active, 12.5 Hz, BDU
LPS25H_PRESS_OUT_XL = 0x28
LPS25H_PRESS_OUT_L = 0x29
LPS25H_PRESS_OUT_H = 0x2A
LPS25H_TEMP_OUT_L = 0x2B
LPS25H_TEMP_OUT_H = 0x2C

# Shared sensitivity
ACCEL_SENSITIVITY_G = 0.061 / 1000.0   # 0.061 mg/LSB at ±2 g (both chips)
GYRO_SENSITIVITY_DPS = 8.75 / 1000.0   # 8.75 mdps/LSB at 245 dps (both chips)


@dataclass
class IMUSnapshot:
    timestamp: str
    accel_x: float
    accel_y: float
    accel_z: float
    magnitude_2d: float
    is_harsh_event: bool
    gyro_x: float | None = None
    gyro_y: float | None = None
    gyro_z: float | None = None
    heading_deg: float | None = None
    pressure_hpa: float | None = None
    temperature_c: float | None = None


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
    """Auto-detects LSM6DSL (6-DOF) or LSM9DS1 + LPS25H (10-DOF) and reads all available sensors."""

    def __init__(self, bus_num: int = 1) -> None:
        self.bus_num = bus_num
        self.harsh_threshold_g = float(os.getenv("HARSH_EVENT_G_THRESHOLD", "0.4"))
        self.gravity_baseline_g = float(os.getenv("GRAVITY_BASELINE_G", "1.0"))
        self._imu_address: int = LSM6DSL_ADDRESS
        self._chip: str = "lsm6dsl"
        self._has_mag: bool = False
        self._has_baro: bool = False

    def connect(self) -> bool:
        """Probe and configure all available sensors. Returns True if accel/gyro is found."""
        accel_found = self._connect_accel_gyro()
        if not accel_found:
            return False
        self._has_mag = self._connect_magnetometer()
        self._has_baro = self._connect_barometer()
        return True

    def _connect_accel_gyro(self) -> bool:
        candidates = (
            (LSM9DS1_AG_ADDRESS, LSM9DS1_AG_WHO_AM_I_VALUE, "lsm9ds1"),
            (LSM6DSL_ADDRESS, LSM6DSL_WHO_AM_I_VALUE, "lsm6dsl"),
        )
        for address, expected, chip_name in candidates:
            try:
                whoami = read_i2c_atomic(self.bus_num, address, WHO_AM_I, 1)[0]
                if whoami != expected:
                    logger.debug("0x%02X WHO_AM_I=0x%02X, expected 0x%02X", address, whoami, expected)
                    continue
                self._imu_address = address
                self._chip = chip_name
                with SMBus(self.bus_num) as bus:
                    if chip_name == "lsm9ds1":
                        bus.write_byte_data(address, LSM9DS1_CTRL_REG1_G, 0x60)   # 119 Hz, 245 dps
                        bus.write_byte_data(address, LSM9DS1_CTRL_REG6_XL, 0x60)  # 119 Hz, ±2 g
                    else:
                        bus.write_byte_data(address, LSM6DSL_CTRL1_XL, 0x50)  # 208 Hz, ±2 g
                        bus.write_byte_data(address, LSM6DSL_CTRL2_G, 0x50)   # 208 Hz, 245 dps
                logger.info(
                    "IMU %s at 0x%02X on bus %d. Threshold=%.2fg, baseline=%.2fg",
                    chip_name.upper(), address, self.bus_num,
                    self.harsh_threshold_g, self.gravity_baseline_g,
                )
                return True
            except OSError:
                continue
        logger.error("No IMU found on bus %d (tried 0x%02X, 0x%02X)", self.bus_num, LSM9DS1_AG_ADDRESS, LSM6DSL_ADDRESS)
        return False

    def _connect_magnetometer(self) -> bool:
        try:
            whoami = read_i2c_atomic(self.bus_num, LSM9DS1_MAG_ADDRESS, WHO_AM_I, 1)[0]
            if whoami != LSM9DS1_MAG_WHO_AM_I_VALUE:
                return False
            with SMBus(self.bus_num) as bus:
                bus.write_byte_data(LSM9DS1_MAG_ADDRESS, LSM9DS1_CTRL_REG1_M, 0xF8)  # temp-comp, ultra, 80 Hz
                bus.write_byte_data(LSM9DS1_MAG_ADDRESS, LSM9DS1_CTRL_REG2_M, 0x00)  # ±4 G
                bus.write_byte_data(LSM9DS1_MAG_ADDRESS, LSM9DS1_CTRL_REG3_M, 0x00)  # continuous
                bus.write_byte_data(LSM9DS1_MAG_ADDRESS, LSM9DS1_CTRL_REG4_M, 0x0C)  # z ultra
            logger.info("Magnetometer (LSM9DS1) initialized at 0x%02X.", LSM9DS1_MAG_ADDRESS)
            return True
        except OSError:
            return False

    def _connect_barometer(self) -> bool:
        try:
            whoami = read_i2c_atomic(self.bus_num, LPS25H_ADDRESS, WHO_AM_I, 1)[0]
            if whoami != LPS25H_WHO_AM_I_VALUE:
                return False
            with SMBus(self.bus_num) as bus:
                bus.write_byte_data(LPS25H_ADDRESS, LPS25H_CTRL_REG1, 0xB0)  # active, 12.5 Hz, BDU
            logger.info("Barometer (LPS25H) initialized at 0x%02X.", LPS25H_ADDRESS)
            return True
        except OSError:
            return False

    def _read_word_2c(self, address: int, register: int) -> int:
        raw = read_i2c_atomic(self.bus_num, address, register, 2)
        value = (raw[1] << 8) | raw[0]
        return value - 65536 if value >= 32768 else value

    def get_acceleration(self) -> tuple[float, float, float]:
        reg = LSM9DS1_OUT_X_L_XL if self._chip == "lsm9ds1" else LSM6DSL_OUTX_L_XL
        x = self._read_word_2c(self._imu_address, reg) * ACCEL_SENSITIVITY_G
        y = self._read_word_2c(self._imu_address, reg + 2) * ACCEL_SENSITIVITY_G
        z = self._read_word_2c(self._imu_address, reg + 4) * ACCEL_SENSITIVITY_G
        return x, y, z

    def get_gyro(self) -> tuple[float, float, float]:
        reg = LSM9DS1_OUT_X_L_G if self._chip == "lsm9ds1" else LSM6DSL_OUTX_L_G
        x = self._read_word_2c(self._imu_address, reg) * GYRO_SENSITIVITY_DPS
        y = self._read_word_2c(self._imu_address, reg + 2) * GYRO_SENSITIVITY_DPS
        z = self._read_word_2c(self._imu_address, reg + 4) * GYRO_SENSITIVITY_DPS
        return x, y, z

    def get_magnetometer(self) -> tuple[float, float, float] | None:
        if not self._has_mag:
            return None
        try:
            x = self._read_word_2c(LSM9DS1_MAG_ADDRESS, LSM9DS1_OUT_X_L_M) * LSM9DS1_MAG_SENSITIVITY
            y = self._read_word_2c(LSM9DS1_MAG_ADDRESS, LSM9DS1_OUT_X_L_M + 2) * LSM9DS1_MAG_SENSITIVITY
            z = self._read_word_2c(LSM9DS1_MAG_ADDRESS, LSM9DS1_OUT_X_L_M + 4) * LSM9DS1_MAG_SENSITIVITY
            return x, y, z
        except OSError:
            return None

    def get_barometer(self) -> tuple[float, float] | None:
        if not self._has_baro:
            return None
        try:
            xl = read_i2c_atomic(self.bus_num, LPS25H_ADDRESS, LPS25H_PRESS_OUT_XL, 1)[0]
            l = read_i2c_atomic(self.bus_num, LPS25H_ADDRESS, LPS25H_PRESS_OUT_L, 1)[0]
            h = read_i2c_atomic(self.bus_num, LPS25H_ADDRESS, LPS25H_PRESS_OUT_H, 1)[0]
            pressure_hpa = ((h << 16) | (l << 8) | xl) / 4096.0

            t_lo = read_i2c_atomic(self.bus_num, LPS25H_ADDRESS, LPS25H_TEMP_OUT_L, 1)[0]
            t_hi = read_i2c_atomic(self.bus_num, LPS25H_ADDRESS, LPS25H_TEMP_OUT_H, 1)[0]
            temp_raw = (t_hi << 8) | t_lo
            if temp_raw >= 32768:
                temp_raw -= 65536
            temperature_c = 42.5 + temp_raw / 480.0
            return pressure_hpa, temperature_c
        except OSError:
            return None

    async def read_loop(
        self,
        snapshot_callback: Callable[[ImuSnapshot], Awaitable[None]],
        harsh_event_callback: Callable[[ImuSnapshot], Awaitable[None]],
        sample_interval_seconds: float = 0.1,
    ) -> None:
        snapshot_interval_seconds = 1.0
        debounce_seconds = 2.0
        last_snapshot_time = 0.0

        while not self.connect():
            await asyncio.sleep(5)

        while True:
            try:
                accel_x, accel_y, accel_z = await asyncio.to_thread(self.get_acceleration)
                magnitude_2d = math.sqrt(accel_x**2 + accel_y**2)
                magnitude_3d = math.sqrt(accel_x**2 + accel_y**2 + accel_z**2)
                dynamic_force_g = max(0.0, magnitude_3d - self.gravity_baseline_g)
                is_harsh = dynamic_force_g >= self.harsh_threshold_g

                current_time = asyncio.get_running_loop().time()
                at_snapshot_time = (current_time - last_snapshot_time >= snapshot_interval_seconds)

                # Read gyro, mag, baro only at 1 Hz or on harsh events to reduce I2C traffic.
                gyro_x = gyro_y = gyro_z = None
                heading_deg = pressure_hpa = temperature_c = None
                if at_snapshot_time or is_harsh:
                    try:
                        gx, gy, gz = await asyncio.to_thread(self.get_gyro)
                        gyro_x, gyro_y, gyro_z = round(gx, 3), round(gy, 3), round(gz, 3)
                    except OSError:
                        pass
                    if self._has_mag:
                        mag = await asyncio.to_thread(self.get_magnetometer)
                        if mag is not None:
                            heading_deg = round(math.degrees(math.atan2(mag[1], mag[0])) % 360, 1)
                    if self._has_baro:
                        baro = await asyncio.to_thread(self.get_barometer)
                        if baro is not None:
                            pressure_hpa = round(baro[0], 2)
                            temperature_c = round(baro[1], 2)

                snapshot = ImuSnapshot(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    accel_x=round(accel_x, 4),
                    accel_y=round(accel_y, 4),
                    accel_z=round(accel_z, 4),
                    magnitude_2d=round(magnitude_2d, 4),
                    is_harsh_event=is_harsh,
                    gyro_x=gyro_x,
                    gyro_y=gyro_y,
                    gyro_z=gyro_z,
                    heading_deg=heading_deg,
                    pressure_hpa=pressure_hpa,
                    temperature_c=temperature_c,
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

                if at_snapshot_time:
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
