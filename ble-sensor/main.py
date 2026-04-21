import asyncio
import hashlib
import hmac
import json
import os
import random
import sqlite3 as _sqlite3
from urllib.parse import quote as _urlquote
import uuid
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
    local_db_path: str
    upload_batch_size: int
    telematics_db_path: str
    pi_location_db_path: str
    pi_location_cache_path: str
    pi_location_query_timeout_seconds: float
    pi_location_stale_after_seconds: int
    pi_id: str
    tracked_asset_registry_path: str
    resolver_candidate_window_seconds: int
    resolver_tie_epsilon: float
    resolver_stationary_mode_enabled: bool
    upload_batch_max_bytes: int
    upload_backoff_initial_seconds: int
    upload_backoff_max_seconds: int


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
            int(os.getenv("SCAN_CONTENTION_MAX_ATTEMPTS_BEFORE_RESET", "10")),
        ),
        local_db_path=os.getenv("BLE_LOCAL_DB_PATH", "/data/ble-sensor.db"),
        upload_batch_size=max(1, int(os.getenv("UPLOAD_BATCH_SIZE", "25"))),
        telematics_db_path=os.getenv("TELEMATICS_DB_PATH", "/data/telematics.db"),
        pi_location_db_path=os.getenv(
            "PI_LOCATION_DB_PATH",
            os.getenv("TELEMATICS_DB_PATH", "/data/telematics.db"),
        ),
        pi_location_cache_path=os.getenv(
            "PI_LOCATION_CACHE_PATH",
            "/data/telematics_last_locked.json",
        ),
        pi_location_query_timeout_seconds=max(
            0.05,
            float(os.getenv("PI_LOCATION_QUERY_TIMEOUT_SECONDS", "0.25")),
        ),
        pi_location_stale_after_seconds=max(
            30,
            int(os.getenv("PI_LOCATION_STALE_AFTER_SECONDS", "180")),
        ),
        pi_id=os.getenv("PI_ID") or os.getenv("HOSTNAME") or "unknown-pi",
        tracked_asset_registry_path=os.getenv(
            "TRACKED_ASSET_REGISTRY_PATH",
            "/data/tracked-assets-registry.json",
        ),
        resolver_candidate_window_seconds=max(
            5,
            int(os.getenv("RESOLVER_CANDIDATE_WINDOW_SECONDS", "60")),
        ),
        resolver_tie_epsilon=max(
            0.0,
            float(os.getenv("RESOLVER_TIE_EPSILON", "0.02")),
        ),
        resolver_stationary_mode_enabled=_to_bool(
            os.getenv("RESOLVER_STATIONARY_MODE_ENABLED", "false"),
            default=False,
        ),
        upload_batch_max_bytes=max(1024, int(os.getenv("UPLOAD_BATCH_MAX_BYTES", "200000"))),
        upload_backoff_initial_seconds=max(1, int(os.getenv("UPLOAD_BACKOFF_INITIAL_SECONDS", "5"))),
        upload_backoff_max_seconds=max(5, int(os.getenv("UPLOAD_BACKOFF_MAX_SECONDS", "300"))),
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


def _canonicalize_mac_address(mac_address: str) -> str:
    """Return MAC as canonical uppercase colon-delimited hex when possible."""
    stripped = "".join(ch for ch in (mac_address or "") if ch.isalnum())
    if len(stripped) == 12 and all(ch in "0123456789abcdefABCDEF" for ch in stripped):
        octets = [stripped[i : i + 2].upper() for i in range(0, 12, 2)]
        return ":".join(octets)

    return (mac_address or "").upper()


def _normalize_mac(mac_address: str, config: Config) -> str:
    """
    Return a stable, privacy-preserving identifier for each device by default.
    Set ANONYMIZE_MAC=false to send raw MAC addresses when explicitly required.
    """
    canonical_mac = _canonicalize_mac_address(mac_address)
    if not config.anonymize_mac:
        return canonical_mac

    digest = hmac.new(
        key=config.mac_hash_salt.encode("utf-8"),
        msg=canonical_mac.lower().encode("utf-8"),
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

    oui = _canonicalize_mac_address(mac_address)[:8]
    return KNOWN_OUIS.get(oui, "Unknown")


async def push_to_cloud(
    session: aiohttp.ClientSession, payload: dict[str, Any], config: Config
) -> bool:
    if not config.webhook_url:
        return False

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
                return False
            print(f"Payload transmitted. Status: {response.status}")
            return True
    except Exception as exc:
        print(f"Transmission failed: {exc}")
        return False


def _init_local_store(db_path: str) -> None:
    with _sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ble_scan_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at_utc TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                sent_at_utc TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                next_retry_at_utc TEXT
            );
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ble_scan_events_pending
            ON ble_scan_events(sent_at_utc, captured_at_utc);
            """
        )
        _ensure_column(conn, "ble_scan_events", "next_retry_at_utc", "TEXT")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ble_scan_events_retry
            ON ble_scan_events(sent_at_utc, next_retry_at_utc, captured_at_utc);
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ble_device_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at_utc TEXT NOT NULL,
                pi_id TEXT NOT NULL,
                vehicle_id TEXT NOT NULL,
                tracked_mac_normalized TEXT NOT NULL,
                rssi INTEGER,
                latitude REAL,
                longitude REAL,
                gps_timestamp TEXT,
                gps_age_seconds INTEGER
            );
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ble_observations_mac_time
            ON ble_device_observations(tracked_mac_normalized, captured_at_utc DESC);
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_asset_registry (
                tracked_mac_normalized TEXT PRIMARY KEY,
                label TEXT,
                active INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_asset_resolutions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at_utc TEXT NOT NULL,
                tracked_mac_normalized TEXT NOT NULL,
                vehicle_id TEXT NOT NULL,
                resolved_pi_id TEXT,
                resolved_latitude REAL,
                resolved_longitude REAL,
                resolution_method TEXT NOT NULL,
                candidate_count INTEGER NOT NULL,
                confidence_score REAL NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tracked_asset_resolutions_lookup
            ON tracked_asset_resolutions(tracked_mac_normalized, captured_at_utc DESC);
            """
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_column(conn: _sqlite3.Connection, table_name: str, column_name: str, column_def: str) -> None:
    existing_columns = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table_name})")  # nosec B608
    }
    if column_name not in existing_columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")  # nosec B608


def _parse_iso_utc(raw: str | None) -> datetime | None:
    if not raw:
        return None

    normalized = raw.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_tracked_assets_from_registry(path: str) -> list[tuple[str, str | None, int]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as registry_file:
        raw = json.load(registry_file)
    if not isinstance(raw, list):
        return []

    rows: list[tuple[str, str | None, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        mac = _canonicalize_mac_address(str(item.get("ble_mac_normalized", "")))
        if len(mac) != 17:
            continue
        label = item.get("label")
        active = 1 if item.get("active", True) else 0
        rows.append((mac, str(label) if label is not None else None, active))
    return rows


def _sync_tracked_asset_registry(config: Config) -> None:
    tracked_rows = _load_tracked_assets_from_registry(config.tracked_asset_registry_path)
    if not tracked_rows:
        return

    with _sqlite3.connect(config.local_db_path, timeout=30.0, isolation_level="IMMEDIATE") as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executemany(
            """
            INSERT INTO tracked_asset_registry (tracked_mac_normalized, label, active)
            VALUES (?, ?, ?)
            ON CONFLICT(tracked_mac_normalized)
            DO UPDATE SET label = excluded.label, active = excluded.active
            """,
            tracked_rows,
        )


def _record_scan_observations(db_path: str, payload: dict[str, Any]) -> None:
    captured_at_utc = str(payload.get("captured_at_utc") or _utc_now_iso())
    pi_id = str(payload.get("pi_id") or "unknown-pi")
    vehicle_id = str(payload.get("vehicle_id") or "UNKNOWN_TRUCK")
    pi_location = payload.get("pi_location") if isinstance(payload.get("pi_location"), dict) else {}
    latitude = pi_location.get("latitude") if isinstance(pi_location, dict) else None
    longitude = pi_location.get("longitude") if isinstance(pi_location, dict) else None
    gps_timestamp = pi_location.get("gps_timestamp") if isinstance(pi_location, dict) else None
    gps_age_seconds = pi_location.get("location_age_sec") if isinstance(pi_location, dict) else None

    rows: list[tuple[Any, ...]] = []
    sensors = payload.get("sensors")
    if not isinstance(sensors, list):
        return
    for sensor in sensors:
        if not isinstance(sensor, dict):
            continue
        normalized_mac = _canonicalize_mac_address(str(sensor.get("mac_address", "")))
        if len(normalized_mac) != 17:
            continue
        rows.append(
            (
                captured_at_utc,
                pi_id,
                vehicle_id,
                normalized_mac,
                sensor.get("rssi"),
                latitude,
                longitude,
                gps_timestamp,
                gps_age_seconds,
            )
        )
    if not rows:
        return

    with _sqlite3.connect(db_path, timeout=30.0, isolation_level="IMMEDIATE") as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executemany(
            """
            INSERT INTO ble_device_observations (
                captured_at_utc, pi_id, vehicle_id, tracked_mac_normalized, rssi,
                latitude, longitude, gps_timestamp, gps_age_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def _score_candidate(
    *,
    rssi: int | None,
    scan_age_seconds: float,
    gps_age_seconds: int | None,
    candidate_window_seconds: int,
    gps_stale_after_seconds: int,
) -> float:
    bounded_rssi = max(-100, min(-30, int(rssi if rssi is not None else -100)))
    rssi_score = (bounded_rssi + 100) / 70
    scan_score = max(0.0, 1.0 - (scan_age_seconds / max(1, candidate_window_seconds)))
    if gps_age_seconds is None:
        gps_score = 0.0
    else:
        gps_score = max(0.0, 1.0 - (gps_age_seconds / max(1, gps_stale_after_seconds)))
    return (0.5 * rssi_score) + (0.3 * scan_score) + (0.2 * gps_score)


def _resolve_tracked_asset_positions(config: Config) -> None:
    now = datetime.now(timezone.utc)
    with _sqlite3.connect(config.local_db_path, timeout=30.0, isolation_level="IMMEDIATE") as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        tracked_assets = conn.execute(
            """
            SELECT tracked_mac_normalized
            FROM tracked_asset_registry
            WHERE active = 1
            """
        ).fetchall()
        for (tracked_mac,) in tracked_assets:
            candidate_rows = conn.execute(
                """
                SELECT id, captured_at_utc, pi_id, rssi, latitude, longitude, gps_age_seconds
                FROM ble_device_observations
                WHERE tracked_mac_normalized = ?
                ORDER BY captured_at_utc DESC, id DESC
                LIMIT 200
                """,
                (tracked_mac,),
            ).fetchall()
            if not candidate_rows:
                continue

            latest_dt = _parse_iso_utc(str(candidate_rows[0][1]))
            if latest_dt is None:
                continue
            candidates: list[dict[str, Any]] = []
            for row in candidate_rows:
                captured_dt = _parse_iso_utc(str(row[1]))
                if captured_dt is None:
                    continue
                age_from_latest = (latest_dt - captured_dt).total_seconds()
                if age_from_latest > config.resolver_candidate_window_seconds:
                    continue
                score = _score_candidate(
                    rssi=int(row[3]) if row[3] is not None else None,
                    scan_age_seconds=max(0.0, (now - captured_dt).total_seconds()),
                    gps_age_seconds=int(row[6]) if row[6] is not None else None,
                    candidate_window_seconds=config.resolver_candidate_window_seconds,
                    gps_stale_after_seconds=config.pi_location_stale_after_seconds,
                )
                candidates.append(
                    {
                        "captured_at_utc": str(row[1]),
                        "pi_id": str(row[2]),
                        "latitude": row[4],
                        "longitude": row[5],
                        "score": score,
                    }
                )
            if not candidates:
                continue

            candidates.sort(key=lambda item: (-item["score"], item["pi_id"], item["captured_at_utc"]))
            winner = candidates[0]
            method = "highest_score"
            resolved_lat = winner["latitude"]
            resolved_lon = winner["longitude"]
            resolved_pi_id = winner["pi_id"]

            if len(candidates) > 1:
                second = candidates[1]
                score_delta = abs(winner["score"] - second["score"])
                if score_delta <= config.resolver_tie_epsilon:
                    pair_has_coords = all(
                        value is not None
                        for value in (
                            winner["latitude"],
                            winner["longitude"],
                            second["latitude"],
                            second["longitude"],
                        )
                    )
                    if config.resolver_stationary_mode_enabled:
                        prior = conn.execute(
                            """
                            SELECT resolved_pi_id, resolved_latitude, resolved_longitude
                            FROM tracked_asset_resolutions
                            WHERE tracked_mac_normalized = ?
                            ORDER BY captured_at_utc DESC, id DESC
                            LIMIT 1
                            """,
                            (tracked_mac,),
                        ).fetchone()
                        if prior:
                            resolved_pi_id = str(prior[0]) if prior[0] is not None else resolved_pi_id
                            resolved_lat = prior[1]
                            resolved_lon = prior[2]
                            method = "stationary_hold"
                    elif pair_has_coords:
                        resolved_lat = (float(winner["latitude"]) + float(second["latitude"])) / 2.0
                        resolved_lon = (float(winner["longitude"]) + float(second["longitude"])) / 2.0
                        method = "centroid_top2"

                    if method not in {"stationary_hold", "centroid_top2"}:
                        method = "lexicographic_pi_tiebreak"
                        resolved_pi_id = min(winner["pi_id"], second["pi_id"])

            confidence_score = max(0.0, min(1.0, float(winner["score"])))
            conn.execute(
                """
                INSERT INTO tracked_asset_resolutions (
                    captured_at_utc, tracked_mac_normalized, vehicle_id, resolved_pi_id,
                    resolved_latitude, resolved_longitude, resolution_method, candidate_count,
                    confidence_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now_iso(),
                    tracked_mac,
                    config.vehicle_id,
                    resolved_pi_id,
                    resolved_lat,
                    resolved_lon,
                    method,
                    len(candidates),
                    confidence_score,
                ),
            )


def _empty_pi_location() -> dict[str, Any]:
    return {
        "latitude": None,
        "longitude": None,
        "fix_status": "stale_or_unknown",
        "gps_timestamp": None,
        "source": "telematics.db:last_locked",
        "location_age_sec": None,
    }


def _pi_location_from_last_locked(
    *,
    latitude: Any,
    longitude: Any,
    gps_timestamp: str | None,
    stale_after_seconds: int,
) -> dict[str, Any]:
    location = _empty_pi_location()
    location["latitude"] = latitude
    location["longitude"] = longitude
    location["gps_timestamp"] = gps_timestamp

    gps_dt = _parse_iso_utc(gps_timestamp)
    if gps_dt is None:
        return location

    location_age_sec = max(0, int((datetime.now(timezone.utc) - gps_dt).total_seconds()))
    location["location_age_sec"] = location_age_sec
    if location_age_sec <= stale_after_seconds:
        location["fix_status"] = "locked"
    return location


def _read_pi_location_from_cache(cache_path: str) -> tuple[Any, Any, str | None] | None:
    if not os.path.exists(cache_path):
        return None

    with open(cache_path, encoding="utf-8") as cache_file:
        payload = json.load(cache_file)

    if not isinstance(payload, dict):
        return None

    latitude = payload.get("latitude")
    if latitude is None:
        latitude = payload.get("lat")

    longitude = payload.get("longitude")
    if longitude is None:
        longitude = payload.get("lon")

    gps_timestamp = payload.get("gps_timestamp") or payload.get("captured_at_utc")
    return latitude, longitude, gps_timestamp


def _read_last_locked_gps_from_db(db_path: str, lock_timeout_seconds: float) -> tuple[Any, Any, str | None] | None:
    if not os.path.exists(db_path):
        return None

    with _sqlite3.connect(db_path, timeout=lock_timeout_seconds) as conn:
        cursor = conn.execute(
            """
            SELECT lat, lon, captured_at_utc, payload_json
            FROM gps_points
            WHERE fix_status = 'locked'
            ORDER BY captured_at_utc DESC, id DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()

    if not row:
        return None

    lat, lon, captured_at_utc, payload_json = row
    gps_timestamp: str | None = captured_at_utc
    try:
        payload = json.loads(payload_json)
        location_payload = payload.get("location") if isinstance(payload, dict) else None
        if isinstance(location_payload, dict):
            gps_timestamp = location_payload.get("gps_timestamp") or gps_timestamp
        elif isinstance(payload, dict):
            gps_timestamp = payload.get("gps_timestamp") or gps_timestamp
    except (TypeError, json.JSONDecodeError):
        pass

    return lat, lon, gps_timestamp


def _read_pi_gps_from_telematics_db(
    db_path: str,
    lock_timeout_seconds: float,
) -> dict[str, Any] | None:
    if not db_path or not os.path.exists(db_path):
        return None

    read_only_uri = f"file:{_urlquote(db_path)}?mode=ro"
    with _sqlite3.connect(
        read_only_uri,
        uri=True,
        timeout=max(0.05, lock_timeout_seconds),
    ) as conn:
        row = conn.execute(
            """
            SELECT lat, lon, captured_at_utc
            FROM gps_points
            WHERE fix_status = 'locked'
            ORDER BY captured_at_utc DESC, id DESC
            LIMIT 1
            """
        ).fetchone()

    if row is None:
        return None

    latitude, longitude, captured_at_utc = row
    if latitude is None or longitude is None:
        return None

    return {
        "latitude": latitude,
        "longitude": longitude,
        "speed_kmh": None,
        "fix_status": "locked",
        "captured_at_utc": captured_at_utc,
    }


async def _collect_pi_location(config: Config) -> dict[str, Any]:
    def _from_cache() -> tuple[Any, Any, str | None] | None:
        return _read_pi_location_from_cache(config.pi_location_cache_path)

    def _from_db() -> tuple[Any, Any, str | None] | None:
        return _read_last_locked_gps_from_db(
            db_path=config.telematics_db_path,
            lock_timeout_seconds=config.pi_location_query_timeout_seconds,
        )

    location: tuple[Any, Any, str | None] | None = None
    try:
        location = await asyncio.wait_for(
            asyncio.to_thread(_from_cache),
            timeout=config.pi_location_query_timeout_seconds,
        )
    except Exception:
        location = None

    if location is None:
        try:
            location = await asyncio.wait_for(
                asyncio.to_thread(_from_db),
                timeout=config.pi_location_query_timeout_seconds,
            )
        except Exception:
            location = None

    if location is None:
        return _empty_pi_location()

    latitude, longitude, gps_timestamp = location
    return _pi_location_from_last_locked(
        latitude=latitude,
        longitude=longitude,
        gps_timestamp=gps_timestamp,
        stale_after_seconds=config.pi_location_stale_after_seconds,
    )


def _queue_scan_payload(db_path: str, payload: dict[str, Any]) -> None:
    with _sqlite3.connect(db_path, timeout=30.0, isolation_level="IMMEDIATE") as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            INSERT INTO ble_scan_events (captured_at_utc, payload_json)
            VALUES (?, ?)
            """,
            (payload["captured_at_utc"], json.dumps(payload)),
        )
    _record_scan_observations(db_path, payload)


def _get_pending_scan_payloads(
    db_path: str, limit: int
) -> list[tuple[int, str, str, int]]:
    now_utc = _utc_now_iso()
    with _sqlite3.connect(db_path, timeout=30.0) as conn:
        cursor = conn.execute(
            """
            SELECT id, captured_at_utc, payload_json, attempts
            FROM ble_scan_events
            WHERE sent_at_utc IS NULL
              AND (next_retry_at_utc IS NULL OR next_retry_at_utc <= ?)
            ORDER BY captured_at_utc ASC, id ASC
            LIMIT ?
            """,
            (now_utc, limit),
        )
        return [
            (int(row[0]), str(row[1]), str(row[2]), int(row[3] or 0))
            for row in cursor.fetchall()
        ]


def _mark_scan_payload_sent(db_path: str, row_id: int) -> None:
    with _sqlite3.connect(db_path, timeout=30.0, isolation_level="IMMEDIATE") as conn:
        conn.execute(
            """
            UPDATE ble_scan_events
            SET sent_at_utc = ?, last_error = NULL, next_retry_at_utc = NULL
            WHERE id = ?
            """,
            (_utc_now_iso(), row_id),
        )


def _mark_scan_payloads_sent(db_path: str, row_ids: list[int]) -> None:
    if not row_ids:
        return
    placeholders = ",".join("?" for _ in row_ids)
    with _sqlite3.connect(db_path, timeout=30.0, isolation_level="IMMEDIATE") as conn:
        conn.execute(
            f"""
            UPDATE ble_scan_events
            SET sent_at_utc = ?, last_error = NULL, next_retry_at_utc = NULL
            WHERE id IN ({placeholders})
            """,
            (_utc_now_iso(), *row_ids),
        )


def _record_scan_upload_failure(
    db_path: str,
    row_id: int,
    error: str,
    *,
    attempts_so_far: int,
    backoff_initial_seconds: int,
    backoff_max_seconds: int,
) -> None:
    backoff_seconds = min(backoff_max_seconds, backoff_initial_seconds * (2 ** max(0, attempts_so_far)))
    retry_at = datetime.now(timezone.utc).timestamp() + backoff_seconds
    next_retry_at_utc = datetime.fromtimestamp(retry_at, tz=timezone.utc).isoformat()
    with _sqlite3.connect(db_path, timeout=30.0, isolation_level="IMMEDIATE") as conn:
        conn.execute(
            """
            UPDATE ble_scan_events
            SET attempts = attempts + 1, last_error = ?, next_retry_at_utc = ?
            WHERE id = ?
            """,
            (error[:500], next_retry_at_utc, row_id),
        )


def _build_scan_batch_payload(
    pending_rows: list[tuple[int, str, str, int]],
    max_event_count: int,
    max_bytes: int,
) -> tuple[list[tuple[int, str, str, int]], dict[str, Any]] | tuple[None, None]:
    selected_rows: list[tuple[int, str, str, int]] = []
    events: list[dict[str, Any]] = []
    for row in pending_rows:
        if len(selected_rows) >= max_event_count:
            break
        _, _, payload_json, _ = row
        try:
            event_payload = json.loads(payload_json)
        except json.JSONDecodeError:
            continue
        candidate_events = [*events, event_payload]
        batch_payload = {
            "event_type": "ble_scan_batch",
            "batch_id": str(uuid.uuid4()),
            "events": candidate_events,
        }
        if len(json.dumps(batch_payload, separators=(",", ":")).encode("utf-8")) > max_bytes:
            if not selected_rows:
                selected_rows.append(row)
                events.append(event_payload)
            break
        selected_rows.append(row)
        events.append(event_payload)
    if not selected_rows:
        return None, None
    return selected_rows, {
        "event_type": "ble_scan_batch",
        "batch_id": str(uuid.uuid4()),
        "events": events,
    }


async def _post_scan_batch(
    session: aiohttp.ClientSession,
    payload: dict[str, Any],
    config: Config,
) -> tuple[bool, set[int] | None]:
    if not config.webhook_url:
        return False, None

    headers = {}
    if config.api_key:
        headers["X-Api-Key"] = config.api_key
    timeout = aiohttp.ClientTimeout(total=config.request_timeout_seconds)
    try:
        async with session.post(config.webhook_url, json=payload, headers=headers, timeout=timeout) as response:
            response_text = await response.text()
            if response.status >= 400:
                print(f"Batch upload failed [{response.status}]: {response_text[:200]}")
                return False, None
            try:
                response_json = json.loads(response_text) if response_text else {}
            except json.JSONDecodeError:
                response_json = {}
            failed_row_ids: set[int] = set()
            raw_failed = response_json.get("failed_row_ids")
            if isinstance(raw_failed, list):
                failed_row_ids = {int(item) for item in raw_failed if str(item).isdigit()}
            print(f"BLE batch transmitted. Status: {response.status}, failed_ids={len(failed_row_ids)}")
            return True, failed_row_ids
    except Exception as exc:
        print(f"Batch transmission failed: {exc}")
        return False, None


async def _flush_pending_scan_payloads(
    session: aiohttp.ClientSession, config: Config
) -> None:
    pending_rows = _get_pending_scan_payloads(config.local_db_path, config.upload_batch_size)
    if not pending_rows:
        return

    for row_id, _, payload_json, attempts in pending_rows:
        try:
            json.loads(payload_json)
        except json.JSONDecodeError as exc:
            _record_scan_upload_failure(
                config.local_db_path,
                row_id,
                f"json_decode_error:{exc}",
                attempts_so_far=attempts,
                backoff_initial_seconds=config.upload_backoff_initial_seconds,
                backoff_max_seconds=config.upload_backoff_max_seconds,
            )

    eligible_rows = _get_pending_scan_payloads(config.local_db_path, config.upload_batch_size)
    selected_rows, batch_payload = _build_scan_batch_payload(
        eligible_rows,
        max_event_count=config.upload_batch_size,
        max_bytes=config.upload_batch_max_bytes,
    )
    if not selected_rows or not batch_payload:
        return

    sent_ok, failed_row_ids = await _post_scan_batch(session, batch_payload, config)
    selected_row_ids = [row[0] for row in selected_rows]
    selected_attempts = {row_id: attempts for row_id, _, _, attempts in selected_rows}

    if sent_ok:
        if failed_row_ids is None:
            failed_row_ids = set()
        success_ids = [row_id for row_id in selected_row_ids if row_id not in failed_row_ids]
        _mark_scan_payloads_sent(config.local_db_path, success_ids)
        for failed_id in failed_row_ids:
            _record_scan_upload_failure(
                config.local_db_path,
                failed_id,
                "batch_partial_failure",
                attempts_so_far=selected_attempts.get(failed_id, 0),
                backoff_initial_seconds=config.upload_backoff_initial_seconds,
                backoff_max_seconds=config.upload_backoff_max_seconds,
            )
        return

    # Backward compatibility fallback for consumers that do not support ble_scan_batch.
    for row_id, captured_at_utc, payload_json, attempts in selected_rows:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            _record_scan_upload_failure(
                config.local_db_path,
                row_id,
                f"json_decode_error:{exc}",
                attempts_so_far=attempts,
                backoff_initial_seconds=config.upload_backoff_initial_seconds,
                backoff_max_seconds=config.upload_backoff_max_seconds,
            )
            continue
        was_uploaded = await push_to_cloud(session, payload, config)
        if was_uploaded:
            _mark_scan_payload_sent(config.local_db_path, row_id)
            continue
        _record_scan_upload_failure(
            config.local_db_path,
            row_id,
            f"single_upload_failed:{captured_at_utc}",
            attempts_so_far=selected_attempts.get(row_id, attempts),
            backoff_initial_seconds=config.upload_backoff_initial_seconds,
            backoff_max_seconds=config.upload_backoff_max_seconds,
        )


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

        canonical_mac_address = _canonicalize_mac_address(device.address)
        sensor_payload = {
            "device_id": _normalize_mac(canonical_mac_address, config),
            "mac_address": canonical_mac_address,
            "rssi": adv_data.rssi,
            "metadata": metadata,
            "device_type": _device_type_from_metadata(canonical_mac_address, metadata),
        }

        if config.include_name:
            sensor_payload["name"] = (
                device.name or adv_data.local_name or "Unknown Broadcast"
            )

        sensors.append(sensor_payload)

    pi_location = await _collect_pi_location(config)
    payload: dict[str, Any] = {
        "vehicle_id": config.vehicle_id,
        "pi_id": config.pi_id,
        "captured_at_utc": _utc_now_iso(),
        "event_type": "ble_sensor_scan",
        "scan_duration_seconds": config.scan_duration_seconds,
        "sensor_count": len(sensors),
        "sensors": sensors,
        "pi_location": pi_location,
    }

    try:
        pi_gps = await asyncio.wait_for(
            asyncio.to_thread(
                _read_pi_gps_from_telematics_db,
                config.telematics_db_path,
                config.pi_location_query_timeout_seconds,
            ),
            timeout=config.pi_location_query_timeout_seconds,
        )
    except Exception:
        pi_gps = None

    if pi_gps is not None:
        payload["pi_gps"] = pi_gps

    return payload


async def run() -> None:
    config = load_config()
    scan_retry_attempt = 0
    _init_local_store(config.local_db_path)
    _sync_tracked_asset_registry(config)

    if config.anonymize_mac and not config.mac_hash_salt:
        print(
            "Warning: ANONYMIZE_MAC=true but MAC_HASH_SALT is empty. "
            "Set a unique secret salt per deployment for stronger privacy guarantees."
        )

    print(
        "BLE scan contention config: "
        f"backoff_cap={config.scan_contention_backoff_cap_seconds}s, "
        f"cooldown_threshold={config.scan_contention_cooldown_threshold}, "
        f"max_attempts_before_reset={config.scan_contention_max_attempts_before_reset}."
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

                _queue_scan_payload(config.local_db_path, payload)
                _sync_tracked_asset_registry(config)
                _resolve_tracked_asset_positions(config)
                print(
                    f"Recorded BLE scan locally (sensor_count={payload['sensor_count']}) "
                    f"for captured_at_utc={payload['captured_at_utc']}."
                )
                await _flush_pending_scan_payloads(session, config)
            except Exception as exc:
                print(f"Scan loop error: {exc}")

            if apply_poll_sleep:
                await asyncio.sleep(config.post_interval_seconds)


if __name__ == "__main__":
    uvloop.install()
    asyncio.run(run())
