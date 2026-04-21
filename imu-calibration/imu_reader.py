import math
import time
from dataclasses import dataclass

from smbus2 import SMBus, i2c_msg

WHO_AM_I = 0x0F
LSM6DSL_ADDRESS = 0x6A
LSM6DSL_WHO_AM_I_VALUE = 0x6A
LSM6DSL_CTRL1_XL = 0x10
LSM6DSL_CTRL2_G = 0x11
LSM6DSL_OUTX_L_G = 0x22
LSM6DSL_OUTX_L_XL = 0x28

LSM9DS1_AG_ADDRESS = 0x6B
LSM9DS1_AG_WHO_AM_I_VALUE = 0x68
LSM9DS1_CTRL_REG1_G = 0x10
LSM9DS1_CTRL_REG6_XL = 0x20
LSM9DS1_OUT_X_L_G = 0x18
LSM9DS1_OUT_X_L_XL = 0x28

ACCEL_SENSITIVITY_G = 0.061 / 1000.0
GYRO_SENSITIVITY_DPS = 8.75 / 1000.0


@dataclass
class IMUSample:
    accel_x: float
    accel_y: float
    accel_z: float
    gyro_x: float
    gyro_y: float
    gyro_z: float
    magnitude_2d: float
    magnitude_3d: float


def read_i2c_atomic(bus_num: int, address: int, register: int, length: int) -> list[int]:
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
    def __init__(self, bus_num: int = 1) -> None:
        self.bus_num = bus_num
        self._imu_address = LSM6DSL_ADDRESS
        self._chip = "lsm6dsl"

    def connect(self) -> bool:
        candidates = (
            (LSM9DS1_AG_ADDRESS, LSM9DS1_AG_WHO_AM_I_VALUE, "lsm9ds1"),
            (LSM6DSL_ADDRESS, LSM6DSL_WHO_AM_I_VALUE, "lsm6dsl"),
        )
        for address, expected, chip_name in candidates:
            try:
                whoami = read_i2c_atomic(self.bus_num, address, WHO_AM_I, 1)[0]
                if whoami != expected:
                    continue
                self._imu_address = address
                self._chip = chip_name
                with SMBus(self.bus_num) as bus:
                    if chip_name == "lsm9ds1":
                        bus.write_byte_data(address, LSM9DS1_CTRL_REG1_G, 0x60)
                        bus.write_byte_data(address, LSM9DS1_CTRL_REG6_XL, 0x60)
                    else:
                        bus.write_byte_data(address, LSM6DSL_CTRL1_XL, 0x50)
                        bus.write_byte_data(address, LSM6DSL_CTRL2_G, 0x50)
                return True
            except OSError:
                continue
        return False

    def _read_word_2c(self, address: int, register: int) -> int:
        raw = read_i2c_atomic(self.bus_num, address, register, 2)
        value = (raw[1] << 8) | raw[0]
        return value - 65536 if value >= 32768 else value

    def _read_accel(self) -> tuple[float, float, float]:
        reg = LSM9DS1_OUT_X_L_XL if self._chip == "lsm9ds1" else LSM6DSL_OUTX_L_XL
        x = self._read_word_2c(self._imu_address, reg) * ACCEL_SENSITIVITY_G
        y = self._read_word_2c(self._imu_address, reg + 2) * ACCEL_SENSITIVITY_G
        z = self._read_word_2c(self._imu_address, reg + 4) * ACCEL_SENSITIVITY_G
        return x, y, z

    def _read_gyro(self) -> tuple[float, float, float]:
        reg = LSM9DS1_OUT_X_L_G if self._chip == "lsm9ds1" else LSM6DSL_OUTX_L_G
        x = self._read_word_2c(self._imu_address, reg) * GYRO_SENSITIVITY_DPS
        y = self._read_word_2c(self._imu_address, reg + 2) * GYRO_SENSITIVITY_DPS
        z = self._read_word_2c(self._imu_address, reg + 4) * GYRO_SENSITIVITY_DPS
        return x, y, z

    def read_sample(self) -> IMUSample:
        ax, ay, az = self._read_accel()
        gx, gy, gz = self._read_gyro()
        magnitude_2d = math.sqrt(ax**2 + ay**2)
        magnitude_3d = math.sqrt(ax**2 + ay**2 + az**2)
        return IMUSample(
            accel_x=round(ax, 5),
            accel_y=round(ay, 5),
            accel_z=round(az, 5),
            gyro_x=round(gx, 5),
            gyro_y=round(gy, 5),
            gyro_z=round(gz, 5),
            magnitude_2d=round(magnitude_2d, 5),
            magnitude_3d=round(magnitude_3d, 5),
        )
