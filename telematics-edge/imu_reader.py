import asyncio
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from smbus2 import SMBus

logger = logging.getLogger(__name__)

# BerryGPS-IMU v4 defaults
LSM9DS1_AG_ADDRESS = 0x6A  # accelerometer + gyroscope
LSM9DS1_MAG_ADDRESS = 0x1C  # magnetometer
LPS25H_ADDRESS = 0x5C  # barometer / temperature

# LSM9DS1 accel/gyro registers
WHO_AM_I_XG = 0x0F
CTRL_REG6_XL = 0x20
CTRL_REG1_G = 0x10
OUT_X_L_G = 0x18
OUT_X_L_XL = 0x28

# LSM9DS1 magnetometer registers
WHO_AM_I_M = 0x0F
CTRL_REG1_M = 0x20
CTRL_REG2_M = 0x21
CTRL_REG3_M = 0x22
OUT_X_L_M = 0x28

# LPS25H barometer registers
WHO_AM_I_LPS25H = 0x0F
CTRL_REG1_LPS25H = 0x20
PRESS_OUT_XL = 0x28
TEMP_OUT_L = 0x2B


@dataclass
class ImuSnapshot:
    timestamp: str
    accel_x_g: float
    accel_y_g: float
    accel_z_g: float
    gyro_x_dps: float
    gyro_y_dps: float
    gyro_z_dps: float
    mag_x_ut: float
    mag_y_ut: float
    mag_z_ut: float
    pressure_hpa: float | None
    temperature_c: float | None
    magnitude_g: float
    dynamic_accel_g: float
    harsh_event_detected: bool
    harsh_event_type: str | None
    calibration: dict[str, Any] = field(default_factory=dict)


class IMUReader:
    """BerryGPS IMU reader for accel/gyro/magnetometer/barometer snapshots."""

    def __init__(self, bus_num: int = 1) -> None:
        self.bus_num = bus_num
        self.bus: SMBus | None = None

        # Event threshold (change from 1g baseline)
        self.threshold_g = float(os.getenv("HARSH_EVENT_G_THRESHOLD", "0.4"))

        # Accel 2g => 0.061 mg/LSB
        self.accel_sensitivity_g = 0.061 / 1000.0
        # Gyro 245 dps => 8.75 mdps/LSB
        self.gyro_sensitivity_dps = 8.75 / 1000.0
        # Mag +/-4 gauss => 0.14 mgauss/LSB ~= 0.014 uT/LSB
        self.mag_sensitivity_ut = 0.014

        # Optional soft calibration offsets (for dashboard alignment/normalization)
        self.accel_offset = {
            "x": float(os.getenv("IMU_ACCEL_OFFSET_X", "0.0")),
            "y": float(os.getenv("IMU_ACCEL_OFFSET_Y", "0.0")),
            "z": float(os.getenv("IMU_ACCEL_OFFSET_Z", "0.0")),
        }
        self.gyro_offset = {
            "x": float(os.getenv("IMU_GYRO_OFFSET_X", "0.0")),
            "y": float(os.getenv("IMU_GYRO_OFFSET_Y", "0.0")),
            "z": float(os.getenv("IMU_GYRO_OFFSET_Z", "0.0")),
        }
        self.mag_offset = {
            "x": float(os.getenv("IMU_MAG_OFFSET_X", "0.0")),
            "y": float(os.getenv("IMU_MAG_OFFSET_Y", "0.0")),
            "z": float(os.getenv("IMU_MAG_OFFSET_Z", "0.0")),
        }

    def connect(self) -> bool:
        try:
            self.bus = SMBus(self.bus_num)

            whoami_xg = self.bus.read_byte_data(LSM9DS1_AG_ADDRESS, WHO_AM_I_XG)
            if whoami_xg != 0x68:
                logger.warning("Unexpected WHO_AM_I_XG value: 0x%02X", whoami_xg)

            whoami_m = self.bus.read_byte_data(LSM9DS1_MAG_ADDRESS, WHO_AM_I_M)
            if whoami_m != 0x3D:
                logger.warning("Unexpected WHO_AM_I_M value: 0x%02X", whoami_m)

            # Accel: 119Hz, +/-2g
            self.bus.write_byte_data(LSM9DS1_AG_ADDRESS, CTRL_REG6_XL, 0x60)
            # Gyro: 119Hz, 245dps
            self.bus.write_byte_data(LSM9DS1_AG_ADDRESS, CTRL_REG1_G, 0x60)

            # Mag: high performance 80Hz, +/-4gauss, continuous conversion
            self.bus.write_byte_data(LSM9DS1_MAG_ADDRESS, CTRL_REG1_M, 0xFC)
            self.bus.write_byte_data(LSM9DS1_MAG_ADDRESS, CTRL_REG2_M, 0x00)
            self.bus.write_byte_data(LSM9DS1_MAG_ADDRESS, CTRL_REG3_M, 0x00)

            # Barometer: power on, 25Hz output
            whoami_baro = self.bus.read_byte_data(LPS25H_ADDRESS, WHO_AM_I_LPS25H)
            if whoami_baro == 0xBD:
                self.bus.write_byte_data(LPS25H_ADDRESS, CTRL_REG1_LPS25H, 0xC0)
            else:
                logger.warning("Unexpected WHO_AM_I_LPS25H value: 0x%02X", whoami_baro)

            logger.info("IMU suite initialized on bus %s", self.bus_num)
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

    def _read_word(self, address: int, register: int) -> int:
        if not self.bus:
            return 0
        low = self.bus.read_byte_data(address, register)
        high = self.bus.read_byte_data(address, register + 1)
        value = (high << 8) | low
        return value - 65536 if value >= 32768 else value

    def _read_baro(self) -> tuple[float | None, float | None]:
        if not self.bus:
            return None, None
        try:
            pxl = self.bus.read_byte_data(LPS25H_ADDRESS, PRESS_OUT_XL)
            pl = self.bus.read_byte_data(LPS25H_ADDRESS, PRESS_OUT_XL + 1)
            ph = self.bus.read_byte_data(LPS25H_ADDRESS, PRESS_OUT_XL + 2)
            pressure_raw = (ph << 16) | (pl << 8) | pxl
            pressure_hpa = pressure_raw / 4096.0

            temp_raw = self._read_word(LPS25H_ADDRESS, TEMP_OUT_L)
            temperature_c = 42.5 + (temp_raw / 480.0)
            return pressure_hpa, temperature_c
        except Exception:  # pylint: disable=broad-except
            return None, None

    async def read_loop(
        self,
        snapshot_callback: Callable[[ImuSnapshot], Awaitable[None]],
        harsh_event_callback: Callable[[ImuSnapshot], Awaitable[None]],
        sample_interval_seconds: float = 1.0,
    ) -> None:
        while True:
            if not self.bus and not self.connect():
                await asyncio.sleep(5)
                continue

            try:
                ax_raw = await asyncio.to_thread(self._read_word, LSM9DS1_AG_ADDRESS, OUT_X_L_XL)
                ay_raw = await asyncio.to_thread(self._read_word, LSM9DS1_AG_ADDRESS, OUT_X_L_XL + 2)
                az_raw = await asyncio.to_thread(self._read_word, LSM9DS1_AG_ADDRESS, OUT_X_L_XL + 4)

                gx_raw = await asyncio.to_thread(self._read_word, LSM9DS1_AG_ADDRESS, OUT_X_L_G)
                gy_raw = await asyncio.to_thread(self._read_word, LSM9DS1_AG_ADDRESS, OUT_X_L_G + 2)
                gz_raw = await asyncio.to_thread(self._read_word, LSM9DS1_AG_ADDRESS, OUT_X_L_G + 4)

                mx_raw = await asyncio.to_thread(self._read_word, LSM9DS1_MAG_ADDRESS, OUT_X_L_M)
                my_raw = await asyncio.to_thread(self._read_word, LSM9DS1_MAG_ADDRESS, OUT_X_L_M + 2)
                mz_raw = await asyncio.to_thread(self._read_word, LSM9DS1_MAG_ADDRESS, OUT_X_L_M + 4)

                pressure_hpa, temperature_c = await asyncio.to_thread(self._read_baro)

                ax = (ax_raw * self.accel_sensitivity_g) - self.accel_offset["x"]
                ay = (ay_raw * self.accel_sensitivity_g) - self.accel_offset["y"]
                az = (az_raw * self.accel_sensitivity_g) - self.accel_offset["z"]

                gx = (gx_raw * self.gyro_sensitivity_dps) - self.gyro_offset["x"]
                gy = (gy_raw * self.gyro_sensitivity_dps) - self.gyro_offset["y"]
                gz = (gz_raw * self.gyro_sensitivity_dps) - self.gyro_offset["z"]

                mx = (mx_raw * self.mag_sensitivity_ut) - self.mag_offset["x"]
                my = (my_raw * self.mag_sensitivity_ut) - self.mag_offset["y"]
                mz = (mz_raw * self.mag_sensitivity_ut) - self.mag_offset["z"]

                magnitude_g = math.sqrt(ax**2 + ay**2 + az**2)
                dynamic_accel_g = magnitude_g - 1.0
                is_harsh = abs(dynamic_accel_g) > self.threshold_g

                event_type = None
                if is_harsh:
                    event_type = "harsh_acceleration" if dynamic_accel_g > 0 else "harsh_braking"

                snapshot = ImuSnapshot(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    accel_x_g=round(ax, 4),
                    accel_y_g=round(ay, 4),
                    accel_z_g=round(az, 4),
                    gyro_x_dps=round(gx, 2),
                    gyro_y_dps=round(gy, 2),
                    gyro_z_dps=round(gz, 2),
                    mag_x_ut=round(mx, 2),
                    mag_y_ut=round(my, 2),
                    mag_z_ut=round(mz, 2),
                    pressure_hpa=round(pressure_hpa, 3) if pressure_hpa is not None else None,
                    temperature_c=round(temperature_c, 2) if temperature_c is not None else None,
                    magnitude_g=round(magnitude_g, 4),
                    dynamic_accel_g=round(dynamic_accel_g, 4),
                    harsh_event_detected=is_harsh,
                    harsh_event_type=event_type,
                    calibration={
                        "tool": "imu_calibration",
                        "reference": os.getenv(
                            "IMU_CALIBRATION_TOOL_URL",
                            "https://fleet-dashboard-464563024600.us-central1.run.app/imu_calibration",
                        ),
                        "accel_offset": self.accel_offset,
                        "gyro_offset": self.gyro_offset,
                        "mag_offset": self.mag_offset,
                    },
                )

                await snapshot_callback(snapshot)
                if is_harsh:
                    logger.warning(
                        "HARSH EVENT DETECTED: %s (dynamic_accel=%0.3fg threshold=%0.3fg)",
                        event_type,
                        dynamic_accel_g,
                        self.threshold_g,
                    )
                    await harsh_event_callback(snapshot)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("IMU read error: %s", exc)
                self._close_bus()

            await asyncio.sleep(sample_interval_seconds)


def snapshot_as_dict(snapshot: ImuSnapshot) -> dict[str, Any]:
    return asdict(snapshot)
