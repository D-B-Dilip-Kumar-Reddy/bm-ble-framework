"""
Start recording, wait, then stop recording — verifying each action via
CameraSession's echo-based confirmation (CLAUDE.md design principle 3).

record_start()/record_stop() raise BMDVerificationError if the camera's
INCOMING_CONTROL echo doesn't arrive or doesn't confirm the expected state
within the session's echo timeout — this script never assumes success from
"the write didn't raise."

Usage:
    python examples/record_start_stop.py

Edit MODEL_KEY / FIRMWARE / RECORD_SECONDS below to target a different
camera or change how long it records for.
"""

import asyncio
import logging

from bmd_ble import BMDVerificationError, CameraSession

MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v7.9"
# MODEL_KEY = "POCKET_6K_PRO"
# FIRMWARE = "v8.6"

RECORD_SECONDS = 5


async def main() -> None:
    async with CameraSession(MODEL_KEY, FIRMWARE) as session:
        try:
            await session.record_start()
        except BMDVerificationError as exc:
            print(f"Record start NOT confirmed: {exc}")
            return
        print("Recording started — confirmed by echo ✓")

        await asyncio.sleep(RECORD_SECONDS)

        try:
            await session.record_stop()
        except BMDVerificationError as exc:
            print(f"Record stop NOT confirmed: {exc}")
            return
        print("Recording stopped — confirmed by echo ✓")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
