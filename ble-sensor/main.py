import asyncio
import os

import aiohttp
from bleak import BleakScanner

WEBHOOK_URL = os.getenv("WEBHOOK_URL")
VEHICLE_ID = os.getenv("VEHICLE_ID", "UNKNOWN_TRUCK")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 60))
API_KEY = os.getenv("API_KEY", "")


async def push_to_cloud(session, payload):
    if not WEBHOOK_URL:
        print("Error: WEBHOOK_URL not configured.")
        return

    try:
        headers = {"X-Api-Key": API_KEY}
        async with session.post(WEBHOOK_URL, json=payload, headers=headers) as response:
            print(f"Payload transmitted. Status: {response.status}")
    except Exception as e:
        print(f"Transmission failed: {e}")


async def run():
    async with aiohttp.ClientSession() as session:
        while True:
            # return_adv=True returns a dictionary mapping MAC -> (BLEDevice, AdvertisementData)
            devices_dict = await BleakScanner.discover(return_adv=True)

            payload = {
                "vehicle_id": VEHICLE_ID,
                "event_type": "ble_sensor_scan",
                "sensors": [],
            }

            for mac, (device, adv_data) in devices_dict.items():
                # Convert raw manufacturer bytes into hex strings for JSON serialization
                mf_data = {}
                if adv_data.manufacturer_data:
                    for company_id, data_bytes in adv_data.manufacturer_data.items():
                        mf_data[str(company_id)] = data_bytes.hex()

                payload["sensors"].append(
                    {
                        "name": device.name or adv_data.local_name or "Unknown Device",
                        "mac_address": device.address,
                        "rssi": adv_data.rssi,
                        "metadata": mf_data,
                    }
                )

            if payload["sensors"]:
                print(f"Detected {len(payload['sensors'])} devices. Initiating upload.")
                await push_to_cloud(session, payload)

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run())
