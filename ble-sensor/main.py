import asyncio
import hashlib
import hmac
import json
import os
import random
import sqlite3 as _sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
import uvloop
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
    key_beacon_uuids: frozenset[str]
    key_beacon_manufacturer_ids: frozenset[int]
    scan_contention_backoff_cap_seconds: float
    scan_contention_cooldown_threshold: int
    scan_contention_max_attempts_before_reset: int


def _to_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> Config:
    poll_interval_seconds = max(5, int(os.getenv("POLL_INTERVAL", "60")))
    scan_duration_seconds = max(1.0, float(os.getenv("SCAN_DURATION_SECONDS", "20")))
    recommended_poll_minimum = scan_duration_seconds + 15
    if poll_interval_seconds < recommended_poll_minimum:
        print(
            "Warning: POLL_INTERVAL is shorter than the recommended minimum for long "
            f"scans. Current={poll_interval_seconds}s, Recommended>="
            f"{recommended_poll_minimum:.0f}s (SCAN_DURATION_SECONDS + 15)."
        )

    raw_uuids = os.getenv("KEY_BEACON_UUIDS", "")
    key_beacon_uuids: frozenset[str] = frozenset(
        u.strip().upper() for u in raw_uuids.split(",") if u.strip()
    )
    raw_mfr = os.getenv("KEY_BEACON_MANUFACTURER_IDS", "")
    key_beacon_manufacturer_ids: frozenset[int] = frozenset(
        int(x.strip()) for x in raw_mfr.split(",") if x.strip().isdigit()
    )

    return Config(
        webhook_url=os.getenv("WEBHOOK_URL"),
        vehicle_id=os.getenv("VEHICLE_ID", "UNKNOWN_TRUCK"),
        post_interval_seconds=poll_interval_seconds,
        scan_duration_seconds=scan_duration_seconds,
        api_key=os.getenv("API_KEY", ""),
        request_timeout_seconds=max(2.0, float(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))),
        anonymize_mac=_to_bool(os.getenv("ANONYMIZE_MAC", "true"), default=True),
        mac_hash_salt=os.getenv("MAC_HASH_SALT", ""),
        include_name=_to_bool(os.getenv("INCLUDE_DEVICE_NAME", "false"), default=False),
        max_devices_per_scan=max(0, int(os.getenv("MAX_DEVICES_PER_SCAN", "0"))),
        key_beacon_uuids=key_beacon_uuids,
        key_beacon_manufacturer_ids=key_beacon_manufacturer_ids,
        scan_contention_backoff_cap_seconds=max(
            2.0,
            float(os.getenv("SCAN_CONTENTION_BACKOFF_CAP_SECONDS", "12")),
        ),
        scan_contention_cooldown_threshold=max(
            1,
            int(os.getenv("SCAN_CONTENTION_COOLDOWN_THRESHOLD", "3")),
        ),
        scan_contention_max_attempts_before_reset=max(
            1,
            int(os.getenv("SCAN_CONTENTION_MAX_ATTEMPTS_BEFORE_RESET", "12")),
        ),
    )


# Expanded OUI prefixes for environmental awareness on BLE scans.
KNOWN_OUIS = {
    "A4:C1:38": "Govee",
    "4C:57:CA": "Apple",
    "04:52:CE": "Apple",
    "00:11:22": "Sensoro",
    "CC:7A:00": "Garmin",
    "00:12:36": "Samsung",
    "D4:36:39": "Tile",
    "00:24:E4": "Withings",
    "00:04:3E": "LG",
    "00:00:F0": "Samsung Electronics Co.,Ltd",
    "00:03:19": "Infineon AG",
    "00:03:89": "Plantronics, Inc.",
    "00:04:3C": "SONOS Co., Ltd.",
    "00:04:A3": "Microchip Technology Inc.",
    "00:09:2D": "HTC Corporation",
    "00:0A:00": "MediaTek Inc",
    "00:0A:F7": "Broadcom",
    "00:0E:DD": "Shure Incorporated",
    "00:0F:AF": "Dialog Inc.",
    "00:12:EE": "Sony Corporation",
    "00:12:F3": "u-blox AG",
    "00:12:FE": "Lenovo Mobile Communication Technology Ltd.",
    "00:13:17": "GN Netcom A/S",
    "00:16:94": "Sennheiser Communications A/S",
    "00:17:A5": "MediaTek Inc",
    "00:17:E3": "Texas Instruments",
    "00:17:E4": "Texas Instruments",
    "00:17:F2": "Apple, Inc.",
    "00:18:00": "Nintendo Co., Ltd.",
    "00:18:2F": "Texas Instruments",
    "00:19:7F": "Plantronics, Inc.",
    "00:1A:75": "Sony Corporation",
    "00:1A:80": "Sony Corporation",
    "00:1C:D7": "Harman/Becker Automotive Systems GmbH",
    "00:1E:C0": "Microchip Technology Inc.",
    "00:20:12": "Beijing Xiaomi Mobile Software Co., Ltd",
    "00:22:D0": "Polar Electro Oy",
    "00:24:E4": "Withings",
    "00:28:E1:4C": "Apple, Inc.",
    "00:34:7E:5C": "Sonos, Inc.",
    "00:38:F0:C8": "Logitech",
    "00:38:F2:3E": "Microsoft Mobile Oy",
    "00:38:F3:2E": "Skullcandy",
    "00:3C:D0:F8": "Apple, Inc.",
    "00:40:82:7B": "STMicroelectronics Rousset SAS",
    "00:44:91:60": "Murata Manufacturing Co., Ltd.",
    "00:48:23:35": "Dialog Semiconductor Hellas SA",
    "00:50:8E:49": "Xiaomi Communications Co Ltd",
    "00:50:8F:4C": "Xiaomi Communications Co Ltd",
    "00:50:92:6A": "Beijing Xiaomi Mobile Software Co., Ltd",
    "00:60:AB:D2": "Bose Corporation",
    "00:64:9C:81": "Qualcomm Inc.",
    "00:6C:D0:32": "LG Electronics",
    "00:74:C2:46": "Amazon Technologies Inc.",
    "00:74:D4:23": "Amazon Technologies Inc.",
    "00:74:D6:37": "Amazon Technologies Inc.",
    "00:80:E1": "STMicroelectronics SRL",
    "00:88:C6:26": "Logitech, Inc",
    "00:90:FB:93": "Renesas Design US Inc.",
    "00:91:EB": "Renesas Electronics (Penang) Sdn. Bhd.",
    "00:A0:50": "Cypress Semiconductor",
    "00:A0:C6": "Qualcomm Inc.",
    "00:BD:3A": "Nokia Corporation",
    "00:B4:63": "Ring LLC",
    "02:2A:7B": "Nintendo Co., Ltd.",
    "04:7A:0B": "Beijing Xiaomi Electronics Co.,Ltd.",
    "04:CF:8C": "XIAOMI Electronics,CO.,LTD",
    "04:E8:B9": "Intel Corporate",
    "08:C8:C2": "GN Audio A/S",
    "08:D1:F9": "Espressif Inc.",
    "08:DF:1F": "Bose Corporation",
    "0B:FC:83": "Airoha Technology Corp.,",
    "0C:41:3E": "Microsoft Corporation",
    "0C:8C:DC": "Suunto Oy",
    "0C:B8:E8": "Renesas Electronics (Penang) Sdn. Bhd.",
    "0C:FC:83": "Airoha Technology Corp.,",
    "10:F1:F2": "LG Electronics (Mobile Communications)",
    "10:F9:6F": "LG Electronics (Mobile Communications)",
    "14:30:C6": "Motorola Mobility LLC, a Lenovo Company",
    "14:36:05": "Nokia Corporation",
    "14:36:C6": "Lenovo Mobile Communication Technology Ltd.",
    "14:8F:21": "Garmin International",
    "14:8F:79": "Apple, Inc.",
    "14:8F:C6": "Apple, Inc.",
    "14:94:6C": "Apple, Inc.",
    "14:95:CE": "Apple, Inc.",
    "18:00:DB": "Fitbit, Inc.",
    "18:19:D6": "Samsung Electronics Co.,Ltd",
    "18:1E:B0": "Samsung Electronics Co.,Ltd",
    "18:20:32": "Apple, Inc.",
    "18:21:95": "Samsung Electronics Co.,Ltd",
    "18:22:7E": "Samsung Electronics Co.,Ltd",
    "18:26:54": "Samsung Electronics Co.,Ltd",
    "18:26:66": "Samsung Electronics Co.,Ltd",
    "18:7F:88": "Ring LLC",
    "20:33:89": "Google, Inc.",
    "20:34:62": "Xiaomi Communications Co Ltd",
    "20:34:FB": "Xiaomi Communications Co Ltd",
    "20:72:A9": "Beijing Xiaomi Electronics Co.,Ltd.",
    "20:76:93": "Lenovo (Beijing) Limited.",
    "20:BA:36": "u-blox AG",
    "20:DE:1E": "Nokia",
    "20:E0:9C": "Nokia",
    "20:EF:BD": "Roku, Inc",
    "24:29:B0": "Huawei Technologies Co.,Ltd",
    "24:2A:EA": "Apple, Inc.",
    "24:2E:02": "Huawei Technologies Co.,Ltd",
    "24:30:F8": "Huawei Device Co., Ltd.",
    "24:3F:AA": "Huawei Device Co., Ltd.",
    "24:44:27": "Huawei Technologies Co.,Ltd",
    "24:45:6B": "Huawei Device Co., Ltd.",
    "24:46:E4": "Huawei Technologies Co.,Ltd",
    "24:48:85": "Huawei Device Co., Ltd.",
    "24:5B:83": "Renesas Electronics (Penang) Sdn. Bhd.",
    "24:AC:AC": "Polar Electro Oy",
    "24:B3:39": "Apple, Inc.",
    "28:11:A5": "Bose Corporation",
    "2C:54:CF": "LG Electronics (Mobile Communications)",
    "2C:59:8A": "LG Electronics (Mobile Communications)",
    "2C:A7:EF": "OnePlus Technology (Shenzhen) Co., Ltd",
    "2C:AA:8E": "Wyze Labs Inc",
    "30:89:A6": "Huawei Technologies Co.,Ltd",
    "30:8D:D4": "Huawei Technologies Co.,Ltd",
    "30:8E:CF": "Huawei Technologies Co.,Ltd",
    "30:90:48": "Apple, Inc.",
    "30:90:AB": "Apple, Inc.",
    "30:96:10": "Huawei Device Co., Ltd.",
    "30:96:3B": "Huawei Device Co., Ltd.",
    "30:A2:C2": "Huawei Device Co., Ltd.",
    "34:08:E1": "Texas Instruments",
    "34:10:5D": "Texas Instruments",
    "34:10:F4": "Silicon Laboratories",
    "34:14:B5": "Texas Instruments",
    "34:90:EA": "Murata Manufacturing Co., Ltd.",
    "38:3D:5B": "Fiberhome Telecommunication Technologies Co.,LTD",
    "38:42:0B": "Sonos, Inc.",
    "38:5B:44": "Silicon Laboratories",
    "38:C1:AC": "Plantronics, Inc.",
    "40:31:3C": "XIAOMI Electronics,CO.,LTD",
    "40:4E:36": "HTC Corporation",
    "40:84:32": "Microchip Technology Inc.",
    "44:1B:F6": "Espressif Inc.",
    "44:73:D6": "Logitech",
    "44:91:60": "Murata Manufacturing Co., Ltd.",
    "48:22:54": "TP-Link Systems Inc",
    "48:3C:0C": "Huawei Technologies Co.,Ltd",
    "48:7B:2F": "Microsoft Corporation",
    "48:86:E8": "Microsoft Corporation",
    "4C:4F:EE": "OnePlus Technology (Shenzhen) Co., Ltd",
    "4C:87:5D": "Bose Corporation",
    "4C:A0:D4": "Telink Semiconductor (Shanghai) Co., Ltd.",
    "50:3C:C4": "Lenovo Mobile Communication Technology Ltd.",
    "54:04:2B": "Lenovo Mobile Communication (Wuhan) Company Limited",
    "54:19:C8": "vivo Mobile Communication Co., Ltd.",
    "54:53:ED": "Sony Corporation",
    "54:64:DE": "u-blox AG",
    "54:F8:2A": "u-blox AG",
    "58:8E:81": "Silicon Laboratories",
    "58:E6:C5": "Espressif Inc.",
    "60:09:C3": "u-blox AG",
    "60:83:73": "Apple, Inc.",
    "60:8B:0E": "Apple, Inc.",
    "60:8C:4A": "Apple, Inc.",
    "60:92:17": "Apple, Inc.",
    "64:7B:D4": "Texas Instruments",
    "64:8C:BB": "Texas Instruments",
    "64:95:6C": "LG Electronics",
    "64:9E:31": "Beijing Xiaomi Mobile Software Co., Ltd",
    "64:BC:0C": "LG Electronics (Mobile Communications)",
    "64:C2:DE": "LG Electronics (Mobile Communications)",
    "68:28:6C": "Sony Interactive Entertainment Inc.",
    "68:B6:B3": "Espressif Inc.",
    "68:B8:BB": "Beijing Xiaomi Electronics Co.,Ltd.",
    "6B:FC:83": "Airoha Technology Corp.,",
    "6C:1D:EB": "u-blox AG",
    "70:72:0D": "Lenovo Mobile Communication Technology Ltd.",
    "74:04:2B": "Lenovo Mobile Communication (Wuhan) Company Limited",
    "74:7A:90": "Murata Manufacturing Co., Ltd.",
    "74:DC:13": "Telink Micro LLC",
    "74:E1:82": "Texas Instruments",
    "74:EF:4B": "Guangdong Oppo Mobile Telecommunications Corp.,Ltd",
    "78:11:DC": "XIAOMI Electronics,CO.,LTD",
    "78:2B:64": "Bose Corporation",
    "78:34:86": "Nokia",
    "78:53:33": "Beijing Xiaomi Electronics Co.,Ltd.",
    "78:A8:73": "Samsung Electronics Co.,Ltd",
    "78:AB:BB": "Samsung Electronics Co.,Ltd",
    "7C:61:93": "HTC Corporation",
    "7C:67:AB": "Roku, Inc",
    "7C:78:B2": "Wyze Labs Inc",
    "7C:C0:AA": "Microsoft Corporation",
    "80:01:5C": "Synaptics, Inc",
    "80:4A:14": "Apple, Inc.",
    "80:4A:F2": "Sonos, Inc.",
    "80:4C:5D": "NXP Semiconductor (Tianjin) LTD.",
    "80:48:2C": "Wyze Labs Inc",
    "80:49:71": "Apple, Inc.",
    "80:50:1B": "Nokia Corporation",
    "80:54:2D": "Samsung Electronics Co.,Ltd",
    "80:54:9C": "Samsung Electronics Co.,Ltd",
    "80:54:E3": "Apple, Inc.",
    "80:57:19": "Samsung Electronics Co.,Ltd",
    "80:58:F8": "Motorola Mobility LLC, a Lenovo Company",
    "84:63:D6": "Microsoft Corporation",
    "84:68:78": "Apple, Inc.",
    "84:F7:03": "Espressif Inc.",
    "88:0F:A2": "Sagemcom Broadband SAS",
    "88:12:4E": "Qualcomm Inc.",
    "88:C6:26": "Logitech, Inc",
    "90:35:EA": "Silicon Laboratories",
    "90:CC:24": "Synaptics, Inc",
    "94:44:44": "LG Innotek",
    "94:65:2D": "OnePlus Technology (Shenzhen) Co., Ltd",
    "94:6A:7C": "OnePlus Technology (Shenzhen) Co., Ltd",
    "94:95:A0": "Google, Inc.",
    "94:9F:3E": "Sonos, Inc.",
    "98:CF:7D": "Apple, Inc.",
    "98:DD:60": "Apple, Inc.",
    "98:E0:D9": "Apple, Inc.",
    "A0:22:DE": "vivo Mobile Communication Co., Ltd.",
    "A0:38:F8": "OURA Health Oy",
    "A0:56:B2": "Harman/Becker Automotive Systems GmbH",
    "A0:88:69": "Intel Corporate",
    "A0:9E:1A": "Polar Electro Oy",
    "A0:BD:71": "QUALCOMM Incorporated",
    "A4:7E:FA": "Withings",
    "A4:8C:DB": "Lenovo",
    "A4:C7:88": "Xiaomi Communications Co Ltd",
    "A4:CC:B3": "Xiaomi Communications Co Ltd",
    "A8:79:8D": "Samsung Electronics Co.,Ltd",
    "A8:86:DD": "Apple, Inc.",
    "A8:88:08": "Apple, Inc.",
    "A8:8C:3E": "Microsoft Corporation",
    "A8:8E:24": "Apple, Inc.",
    "A8:91:3D": "Apple, Inc.",
    "A8:96:8A": "Apple, Inc.",
    "A8:7B:39": "Nokia Corporation",
    "B4:C2:6A": "Garmin International",
    "B8:21:1C": "Apple, Inc.",
    "B8:22:0C": "Apple, Inc.",
    "B8:27:EB": "Raspberry Pi Foundation",
    "B8:2A:A9": "Apple, Inc.",
    "B8:4F:D5": "Microsoft Corporation",
    "B8:5C:5C": "Microsoft Corporation",
    "BC:79:AD": "Samsung Electronics Co.,Ltd",
    "BC:87:FA": "Bose Corporation",
    "BC:7F:A4": "Xiaomi Communications Co Ltd",
    "BC:89:A6": "Nintendo Co., Ltd.",
    "BC:F2:12": "Telink Micro LLC",
    "BC:F2:92": "Plantronics, Inc.",
    "C0:1C:6A": "Google, Inc.",
    "C0:2E:25": "Guangdong Oppo Mobile Telecommunications Corp.,Ltd",
    "C0:28:8D": "Logitech, Inc",
    "C4:19:D1": "Telink Semiconductor (Shanghai) Co., Ltd.",
    "C4:22:4E": "Telink Micro LLC",
    "C4:DE:E2": "Espressif Inc.",
    "C8:02:10": "LG Innotek",
    "C8:3A:6B": "Roku, Inc",
    "C8:B6:FE": "Fitbit, Inc.",
    "C8:DB:26": "Logitech, Inc",
    "CC:DA:20": "Beijing Xiaomi Mobile Software Co., Ltd",
    "CC:D8:43": "Beijing Xiaomi Mobile Software Co., Ltd",
    "CC:DE:DE": "Nokia",
    "CC:F9:57": "u-blox AG",
    "CC:FB:65": "Nintendo Co., Ltd.",
    "CC:FE:3C": "Samsung Electronics Co.,Ltd",
    "CC:FF:90": "Huawei Device Co., Ltd.",
    "D0:03:4B": "Apple, Inc.",
    "D0:03:DF": "Samsung Electronics Co.,Ltd",
    "D0:3F:27": "Wyze Labs Inc",
    "D4:01:29": "Broadcom",
    "D4:04:E6": "Broadcom Limited",
    "D4:43:8A": "Beijing Xiaomi Mobile Software Co., Ltd",
    "D4:48:67": "Silicon Laboratories",
    "D4:53:2A": "Beijing Xiaomi Mobile Software Co., Ltd",
    "D4:53:83": "Murata Manufacturing Co., Ltd.",
    "D8:13:2A": "Espressif Inc.",
    "D8:5F:77": "Telink Semiconductor (Shanghai) Co., Ltd.",
    "E0:2C:B2": "Lenovo Mobile Communication (Wuhan) Company Limited",
    "E0:A3:66": "Motorola Mobility LLC, a Lenovo Company",
    "E0:D4:64": "Intel Corporate",
    "E0:DE:F2": "Texas Instruments",
    "E4:45:19": "Beijing Xiaomi Electronics Co.,Ltd.",
    "E4:5E:37": "Intel Corporate",
    "E4:5F:01": "Raspberry Pi Trading Ltd",
    "E4:60:17": "Intel Corporate",
    "EC:10:55": "Beijing Xiaomi Electronics Co.,Ltd.",
    "EC:41:18": "XIAOMI Electronics,CO.,LTD",
    "EC:81:93": "Logitech, Inc",
    "EC:FA:5C": "Beijing Xiaomi Electronics Co.,Ltd.",
    "F0:6E:0B": "Microsoft Corporation",
    "F0:C8:8B": "Wyze Labs Inc",
    "F0:F6:C1": "Sonos, Inc.",
    "F4:CE:36": "Nordic Semiconductor ASA",
    "F4:F5:D8": "Google, Inc.",
    "F8:0F:F9": "Google, Inc.",
    "F8:10:93": "Apple, Inc.",
    "F8:E0:79": "Motorola Mobility LLC, a Lenovo Company",
    "F8:EF:5D": "Motorola Mobility LLC, a Lenovo Company",
    "F8:F1:B6": "Motorola Mobility LLC, a Lenovo Company",
}


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


def _device_type_from_metadata(mac_address: str, metadata: dict[str, Any]) -> str:
    """
    Enrichment-only tagging:
    - identify vendors by Bluetooth SIG Company IDs in manufacturer_data;
    - fall back to known service UUIDs and payload signatures;
    - then use OUI prefixes as a best-effort guess for static devices;
    - never drop records based on classification.
    """
    mfg_data = metadata.get("manufacturer_data") or {}
    service_uuids = metadata.get("service_uuids") or []

    # Bleak serialization currently stores company IDs as strings.
    # We still normalize defensively in case another caller provides ints.
    mfg_keys = {str(company_id) for company_id in mfg_data.keys()}

    # 1) Bluetooth SIG Company IDs (works even with randomized MAC addresses)
    if "76" in mfg_keys:
        return "Apple"
    if "117" in mfg_keys:
        return "Samsung"
    if "6" in mfg_keys:
        return "Microsoft"
    if "87" in mfg_keys:
        return "Garmin"

    # 2) Service UUID hints for background broadcast features
    normalized_uuids = {str(uuid).lower() for uuid in service_uuids}
    if "0000fcf1-0000-1000-8000-00805f9b34fb" in normalized_uuids:
        return "Google / Android"
    if "0000fe9f-0000-1000-8000-00805f9b34fb" in normalized_uuids:
        return "Garmin"

    # 3) Payload signature decoding for known sensors
    mfg_hex_dump = "".join(str(value) for value in mfg_data.values()).upper()
    if "494E54454C4C49" in mfg_hex_dump:
        return "Govee Temp Sensor"

    metadata_text = json.dumps(metadata).upper()
    if "TEMP_F" in metadata_text:
        return "Govee Temp Sensor"
    if "0201061AFF4C" in mfg_hex_dump:
        return "Apple iBeacon"
    if "0201060303AAFE" in mfg_hex_dump:
        return "Eddystone Beacon"

    oui = mac_address.upper()[:8]
    return KNOWN_OUIS.get(oui, "Unknown")


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


def _write_ble_wake_signal() -> None:
    try:
        with _sqlite3.connect(
            "/data/telematics.db",
            timeout=30.0,
            isolation_level="IMMEDIATE",
        ) as conn:
            conn.execute(
                "INSERT INTO wake_signals (signal_type, created_at) VALUES (?, ?)",
                ("ble_key_beacon", datetime.now(timezone.utc).isoformat()),
            )
        print("Key beacon detected: wake signal written to shared DB.")
    except Exception as exc:  # pylint: disable=broad-except
        print(f"Failed to write BLE wake signal: {exc}")


async def collect_payload(config: Config) -> dict[str, Any]:
    devices = await BleakScanner.discover(
        timeout=config.scan_duration_seconds,
        return_adv=True,
    )

    if config.key_beacon_uuids or config.key_beacon_manufacturer_ids:
        for _, (device, adv_data) in devices.items():
            uuids = {str(u).upper() for u in (adv_data.service_uuids or [])}
            mfr_keys = set((adv_data.manufacturer_data or {}).keys())
            if uuids & config.key_beacon_uuids or mfr_keys & config.key_beacon_manufacturer_ids:
                print(f"Key beacon matched: address={device.address}")
                _write_ble_wake_signal()
                break

    sensors: list[dict[str, Any]] = []
    for _, (device, adv_data) in devices.items():
        if config.max_devices_per_scan and len(sensors) >= config.max_devices_per_scan:
            break

        metadata = _serialize_metadata(adv_data)

        sensor_payload = {
            "device_id": _normalize_mac(device.address, config),
            "mac_address": device.address,
            "rssi": adv_data.rssi,
            "metadata": metadata,
            "device_type": _device_type_from_metadata(device.address, metadata),
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
    scan_retry_attempt = 0

    if config.anonymize_mac and not config.mac_hash_salt:
        print(
            "Warning: ANONYMIZE_MAC=true but MAC_HASH_SALT is empty. "
            "Set a unique secret salt per deployment for stronger privacy guarantees."
        )

    async with aiohttp.ClientSession() as session:
        while True:
            apply_poll_sleep = True
            try:
                try:
                    payload = await collect_payload(config)
                    scan_retry_attempt = 0
                except Exception as exc:
                    err_text = f"{exc}".lower()
                    cause_text = f"{exc.__cause__}".lower() if exc.__cause__ else ""
                    class_text = f"{exc.__class__.__name__} {type(exc).__name__}".lower()
                    combined_error_text = f"{err_text} {cause_text} {class_text}"
                    is_op_in_progress = (
                        "operation already in progress" in combined_error_text
                        or "org.bluez.error.inprogress" in combined_error_text
                    )

                    if is_op_in_progress:
                        apply_poll_sleep = False
                        backoff_seconds = min(
                            config.scan_contention_backoff_cap_seconds,
                            0.75 * (2**scan_retry_attempt),
                        )
                        jitter_seconds = random.uniform(0, 0.5)
                        sleep_seconds = backoff_seconds + jitter_seconds
                        scan_retry_attempt += 1
                        print(
                            "BlueZ scan-start contention detected "
                            f"(attempt {scan_retry_attempt}): {exc}. "
                            f"Retrying in {sleep_seconds:.2f}s."
                        )
                        await asyncio.sleep(sleep_seconds)
                        if scan_retry_attempt % config.scan_contention_cooldown_threshold == 0:
                            print(
                                "Repeated scan contention detected. Applying extended "
                                f"cooldown of {config.post_interval_seconds}s before retry."
                            )
                            await asyncio.sleep(config.post_interval_seconds)

                        if scan_retry_attempt >= config.scan_contention_max_attempts_before_reset:
                            print(
                                "Persistent BlueZ scan contention detected. Resetting retry "
                                f"counter after {config.post_interval_seconds}s cooldown. "
                                "Check for competing BLE scanners on the host."
                            )
                            scan_retry_attempt = 0
                            await asyncio.sleep(config.post_interval_seconds)
                        continue

                    raise

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

            if apply_poll_sleep:
                await asyncio.sleep(config.post_interval_seconds)


if __name__ == "__main__":
    uvloop.install()
    asyncio.run(run())
