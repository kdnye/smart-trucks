import asyncio
import os
import aiohttp
from bleak import BleakScanner

# Pull configuration from Balena Environment Variables
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
VEHICLE_ID = os.getenv("VEHICLE_ID", "UNKNOWN_TRUCK")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 60))

async def push_to_cloud(session, payload):
    if not WEBHOOK_URL:
        print("Error: WEBHOOK_URL not configured.")
        return
    
    try:
        # Pass a secret header if routing through your existing GCP webhook function
        headers = {"X-Api-Key": os.getenv("API_KEY", "")} 
        async with session.post(WEBHOOK_URL, json=payload, headers=headers) as response:
            print(f"Payload transmitted. Status: {response.status}")
    except Exception as e:
        print(f"Transmission failed: {e}")

async def run():
    async with aiohttp.ClientSession() as session:
        while True:
            devices = await BleakScanner.discover()
            
            payload = {
                "vehicle_id": VEHICLE_ID,
                "event_type": "ble_sensor_scan",
                "sensors": []
            }
            
            for d in devices:
                if "GVH" in d.name:
                    payload["sensors"].append({
                        "name": d.name,
                        "mac_address": d.address,
                        "rssi": d.rssi,
                        "metadata": d.metadata
                    })
            
            if payload["sensors"]:
                print(f"Detected {len(payload['sensors'])} sensors. Initiating upload.")
                await push_to_cloud(session, payload)
            
            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(run())
