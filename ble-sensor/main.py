import asyncio
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Any

import aiohttp
from bleak import BleakScanner


@dataclass(frozen=True)
class Config:
    webhook_url: str | None
    vehicle_id: str
    post_interval_seconds: int
    scan_duration_seconds: float
    api_key: str
    request_timeout_seconds: float
    anonymize_mac: bool
    mac_hash_salt: str
    include_name: bool
    max_devices_per_scan: int


def _to_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> Config:
    return Config(
        webhook_url=os.getenv("WEBHOOK_URL"),
        vehicle_id=os.getenv("VEHICLE_ID", "UNKNOWN_TRUCK"),
        post_interval_seconds=max(5, int(os.getenv("POLL_INTERVAL", "60"))),
        scan_duration_seconds=max(1.0, float(os.getenv("SCAN_DURATION_SECONDS", "15"))),
        api_key=os.getenv("API_KEY", ""),
        request_timeout_seconds=max(2.0, float(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))),
        anonymize_mac=_to_bool(os.getenv("ANONYMIZE_MAC", "true"), default=True),
        mac_hash_salt=os.getenv("MAC_HASH_SALT", ""),
        include_name=_to_bool(os.getenv("INCLUDE_DEVICE_NAME", "false"), default=False),
        max_devices_per_scan=max(1, int(os.getenv("MAX_DEVICES_PER_SCAN", "500"))),
    )


def _normalize_mac(mac_address: str, config: Config) -> str:
    """
    Return a stable, privacy-preserving identifier for each device by default.
    Set ANONYMIZE_MAC=false to send raw MAC addresses when explicitly required.
    """
    if not config.anonymize_mac:
        return mac_address

    digest = hmac.new(
        key=config.mac_hash_salt.encode("utf-8"),
        msg=mac_address.lower().encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return digest[:20]


def _serialize_metadata(adv_data: Any) -> dict[str, Any]:
    manufacturer_data = {}
    if adv_data.manufacturer_data:
        manufacturer_data = {
            str(company_id): data_bytes.hex()
            for company_id, data_bytes in adv_data.manufacturer_data.items()
        }

    service_data = {}
    if adv_data.service_data:
        service_data = {
            str(service_id): bytes_data.hex()
            for service_id, bytes_data in adv_data.service_data.items()
        }

    return {
        "manufacturer_data": manufacturer_data,
        "service_uuids": list(adv_data.service_uuids or []),
        "service_data": service_data,
        "tx_power": adv_data.tx_power,
    }


async def push_to_cloud(
    session: aiohttp.ClientSession, payload: dict[str, Any], config: Config
) -> None:
    if not config.webhook_url:
        print("Error: WEBHOOK_URL is not configured.")
        return

    headers = {}
    if config.api_key:
        headers["X-Api-Key"] = config.api_key

    try:
        timeout = aiohttp.ClientTimeout(total=config.request_timeout_seconds)
        async with session.post(
            config.webhook_url,
            json=payload,
            headers=headers,
            timeout=timeout,
        ) as response:
            response_text = await response.text()
            if response.status >= 400:
                print(f"Upload failed [{response.status}]: {response_text[:200]}")
                return
            print(f"Payload transmitted. Status: {response.status}")
    except Exception as exc:
        print(f"Transmission failed: {exc}")


async def collect_payload(config: Config) -> dict[str, Any]:
    devices = await BleakScanner.discover(
        timeout=config.scan_duration_seconds,
        return_adv=True,
    )

    sensors: list[dict[str, Any]] = []
    for _, (device, adv_data) in devices.items():
        if len(sensors) >= config.max_devices_per_scan:
            break

        sensor_payload = {
            "device_id": _normalize_mac(device.address, config),
            "rssi": adv_data.rssi,
            "metadata": _serialize_metadata(adv_data),
        }

        if config.include_name:
            sensor_payload["name"] = (
                device.name or adv_data.local_name or "Unknown Broadcast"
            )

        sensors.append(sensor_payload)

    return {
        "vehicle_id": config.vehicle_id,
        "event_type": "ble_sensor_scan",
        "scan_duration_seconds": config.scan_duration_seconds,
        "sensor_count": len(sensors),
        "sensors": sensors,
    }


async def run() -> None:
    config = load_config()

    if config.anonymize_mac and not config.mac_hash_salt:
        print(
            "Warning: ANONYMIZE_MAC=true but MAC_HASH_SALT is empty. "
            "Set a unique secret salt per deployment for stronger privacy guarantees."
        )

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                payload = await collect_payload(config)
                if payload["sensor_count"] > 0:
                    print(
                        f"Detected {payload['sensor_count']} devices in "
                        f"{config.scan_duration_seconds:.0f}s scan. Uploading..."
                    )
                    await push_to_cloud(session, payload, config)
                else:
                    print("No BLE broadcasts detected in this scan window.")
            except Exception as exc:
                print(f"Scan loop error: {exc}")

            await asyncio.sleep(config.post_interval_seconds)


if __name__ == "__main__":
    asyncio.run(run())
