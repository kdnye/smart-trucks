# ble-sensor/main.py (Simplified)
import asyncio
from bleak import BleakScanner

async def run():
    while True:
        devices = await BleakScanner.discover()
        for d in devices:
            if "GVH" in d.name: # Filter for Govee
                # Parse advertisement data here
                print(f"Found Sensor {d.name}: {d.metadata}")
        await asyncio.sleep(5)

asyncio.run(run())
