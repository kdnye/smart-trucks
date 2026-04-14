from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

from smbus2 import SMBus


@dataclass(frozen=True)
class NmeaProbeResult:
    device: str
    exists: bool
    nmea_detected: bool
    sample: str | None
    error: str | None


@dataclass(frozen=True)
class I2CProbeResult:
    bus: int
    scanned_addresses: tuple[int, ...]
    responsive_addresses: tuple[int, ...]
    ups_expected_addresses: tuple[int, ...]
    ups_found_addresses: tuple[int, ...]
    imu_expected_addresses: tuple[int, ...]
    imu_found_addresses: tuple[int, ...]
    overlap_addresses: tuple[int, ...]
    error: str | None


@dataclass(frozen=True)
class HardwareInventory:
    gps_candidates: tuple[str, ...]
    nmea_probe: tuple[NmeaProbeResult, ...]
    i2c_probe: I2CProbeResult

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_int_env(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        return max(minimum, default)
    try:
        return max(minimum, int(raw.strip()))
    except ValueError:
        return max(minimum, default)


def parse_hex_list_env(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default

    deduped: list[int] = []
    seen: set[int] = set()
    for token in raw.split(","):
        item = token.strip()
        if not item:
            continue
        try:
            value = int(item, 16)
        except ValueError:
            continue
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)

    return tuple(deduped) if deduped else default


def probe_nmea_candidates(
    candidates: tuple[str, ...],
    baud_rate: int,
    probe_timeout_seconds: float = 1.5,
) -> tuple[NmeaProbeResult, ...]:
    try:
        import serial  # type: ignore
    except Exception as exc:  # pylint: disable=broad-except
        return tuple(
            NmeaProbeResult(
                device=device,
                exists=os.path.exists(device),
                nmea_detected=False,
                sample=None,
                error=f"pyserial unavailable: {exc}",
            )
            for device in candidates
        )

    results: list[NmeaProbeResult] = []
    for device in candidates:
        exists = os.path.exists(device)
        if not exists:
            results.append(
                NmeaProbeResult(device=device, exists=False, nmea_detected=False, sample=None, error="missing")
            )
            continue

        sample: str | None = None
        error: str | None = None
        detected = False
        try:
            with serial.Serial(device, baudrate=baud_rate, timeout=0.25) as handle:
                deadline = time.monotonic() + probe_timeout_seconds
                while time.monotonic() < deadline:
                    line = handle.readline().decode("ascii", errors="ignore").strip()
                    if not line:
                        continue
                    sample = line[:120]
                    if line.startswith("$") and "," in line:
                        detected = True
                        break
        except Exception as exc:  # pylint: disable=broad-except
            error = str(exc)

        results.append(
            NmeaProbeResult(
                device=device,
                exists=True,
                nmea_detected=detected,
                sample=sample,
                error=error,
            )
        )

    # Give slower UART drivers time to flush and release file descriptors
    # before the long-running GPS reader reopens the same port.
    time.sleep(1.0)
    return tuple(results)


def _probe_i2c_bus(bus: int, addresses: tuple[int, ...]) -> tuple[tuple[int, ...], str | None]:
    responsive: list[int] = []
    try:
        with SMBus(bus) as smbus:
            for address in addresses:
                try:
                    smbus.read_byte(address)
                    responsive.append(address)
                except OSError:
                    continue
    except Exception as exc:  # pylint: disable=broad-except
        return tuple(), str(exc)
    return tuple(responsive), None


def probe_i2c(
    bus: int,
    ups_expected_addresses: tuple[int, ...],
    imu_expected_addresses: tuple[int, ...],
) -> I2CProbeResult:
    scanned = tuple(dict.fromkeys([*ups_expected_addresses, *imu_expected_addresses]))
    overlap = tuple(sorted(set(ups_expected_addresses).intersection(imu_expected_addresses)))
    responsive, error = _probe_i2c_bus(bus, scanned)
    responsive_set = set(responsive)
    return I2CProbeResult(
        bus=bus,
        scanned_addresses=scanned,
        responsive_addresses=responsive,
        ups_expected_addresses=ups_expected_addresses,
        ups_found_addresses=tuple(address for address in ups_expected_addresses if address in responsive_set),
        imu_expected_addresses=imu_expected_addresses,
        imu_found_addresses=tuple(address for address in imu_expected_addresses if address in responsive_set),
        overlap_addresses=overlap,
        error=error,
    )


def build_hardware_inventory(
    gps_candidates: tuple[str, ...],
    gps_baud_rate: int,
    i2c_bus: int,
    ups_expected_addresses: tuple[int, ...],
    imu_expected_addresses: tuple[int, ...],
) -> HardwareInventory:
    return HardwareInventory(
        gps_candidates=gps_candidates,
        nmea_probe=probe_nmea_candidates(gps_candidates, gps_baud_rate),
        i2c_probe=probe_i2c(i2c_bus, ups_expected_addresses, imu_expected_addresses),
    )


def validate_inventory(inventory: HardwareInventory, *, imu_required: bool, ups_required: bool) -> list[str]:
    errors: list[str] = []
    i2c = inventory.i2c_probe

    if i2c.overlap_addresses:
        overlaps = ", ".join(f"0x{addr:02X}" for addr in i2c.overlap_addresses)
        errors.append(f"UPS/IMU I2C overlap detected: {overlaps}")

    if imu_required and not i2c.imu_found_addresses:
        expected = ", ".join(f"0x{addr:02X}" for addr in i2c.imu_expected_addresses) or "<none>"
        errors.append(f"Required IMU device missing on bus {i2c.bus}; expected one of [{expected}]")

    if ups_required and not i2c.ups_found_addresses:
        expected = ", ".join(f"0x{addr:02X}" for addr in i2c.ups_expected_addresses) or "<none>"
        errors.append(f"Required UPS device missing on bus {i2c.bus}; expected one of [{expected}]")

    if i2c.error:
        errors.append(f"I2C probe failed on bus {i2c.bus}: {i2c.error}")

    return errors
