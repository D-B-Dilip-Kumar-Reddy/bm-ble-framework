import asyncio
import logging

from bmd_ble.scanner import DiscoveredCamera, scan_for_camera

BLE_NAME = "A:AF3DC814"

async def main():
    result = await scan_for_camera(BLE_NAME)
    print(result)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    asyncio.run(main())