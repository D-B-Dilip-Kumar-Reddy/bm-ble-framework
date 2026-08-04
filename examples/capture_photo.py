"""
Trigger a still photo capture over BLE, then confirm it over REST — the
composition `docs/ble/photo_capture.md` §7.3 left as an open TODO, and
`docs/rest/transport.md`/`docs/rest/session.md`'s Phase 6.

WHY BOTH TRANSPORTS ARE OPEN AT ONCE
---------------------------------------
`CameraSession.capture_photo()` (BLE) sends the confirmed trigger
(category `0x0A`/parameter `0x03`/`VOID`) but cannot verify anything itself
— no BLE channel (echo or `CAMERA_STATUS`) has ever been observed to move
in response, on either camera tested (`docs/ble/photo_capture.md` §7, §9).
`rest/media.py` supplies the confirmation BLE structurally cannot: watch the
Stills directory itself change on the SD card, over REST. This script is
therefore the first thing in this codebase to hold a BLE `CameraSession`
and a REST `RestCameraSession` open to the *same physical camera*
simultaneously — an untested combination (the plan's own risk list flags
"concurrent BLE + REST is unverified... Phase 6 needs both open at once —
confirm on hardware"). This run is that confirmation.

WHICH CAMERA/FIRMWARE
-------------------------
The photo trigger is confirmed in the profile for all three currently-verified
profiles: `POCKET_6K_G2 v7.9`, `POCKET_6K_PRO v8.6`, and — as of 2026-08-04,
via `tools/control/verify_photo_trigger.py`'s REST cross-check — `POCKET_6K_G2
v8.6` (`docs/ble/photo_capture.md` §11.4), this codebase's usual primary
reference. Defaults to `POCKET_6K_G2 v8.6` for that reason, matching CLAUDE.md's
"start all new features with POCKET_6K_G2 v8.6" convention — BLE_MODEL_KEY/
BLE_FIRMWARE and HOST/REST_MODEL_KEY/REST_FIRMWARE all target the same
physical camera, just over two different transports.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: takes one real photo.

CONFIRMATION DESIGN — no *guaranteed* filename, but a best-effort guess
-----------------------------------------------------------------------------
`rest/media.py`'s `stills_marker()`/`wait_for_new_still()` confirm *that* a
new still appeared by watching the Stills subdirectory's own `mtime` in the
(working) mount-root listing — Stills' own contents can never be listed
over REST (a permanent firmware `500`, `docs/rest/transport.md`), and an
earlier design that tried to predict a still's exact filename from clip
data was disproven on real hardware (see that module's docstring and
`docs/ble/photo_capture.md` §11). No clip needs to exist on the card first
— the previous design's `clips()` precondition is gone along with the
filename-prediction it fed. Confirmation success/failure never depends on
a filename. After confirming, this script makes one **opt-in, best-effort**
attempt to name the file anyway, via `guess_new_still_path()` — a narrow
probe around the trigger's own timestamp — and reports whatever it finds
(or doesn't) purely as a convenience; a `None` here does not mean the
capture failed.

Usage:
    python examples/capture_photo.py
"""

import asyncio
import logging
import sys
from datetime import datetime

from bmd_camera import BMDUnsupportedError, CameraSession, RestCameraSession
from bmd_camera.exceptions import BMDStorageError
from bmd_camera.rest.media import (
    guess_new_still_path,
    resolve_active_mount,
    stills_marker,
    wait_for_new_still,
)

HOST = "pocket-cinema-camera-6k-g2.local"
REST_MODEL_KEY = "POCKET_6K_G2"
REST_FIRMWARE = "v8.6"
BLE_MODEL_KEY = "POCKET_6K_G2"
BLE_FIRMWARE = "v8.6"
# HOST = "pocket-cinema-camera-6k-pro.local"
# REST_MODEL_KEY = "POCKET_6K_PRO"
# REST_FIRMWARE = "v8.6"
# BLE_MODEL_KEY = "POCKET_6K_PRO"
# BLE_FIRMWARE = "v8.6"

CONFIRM_TIMEOUT_S = 15.0


async def main() -> int:
    async with RestCameraSession(HOST, REST_MODEL_KEY, REST_FIRMWARE) as rest_session:
        storage = await rest_session.storage_state()
        if storage.active_device is None:
            raise BMDStorageError(f"[{HOST}] No active storage device — cannot capture a photo")
        print(f"Active storage: {storage.active_device.device_name}")

        mount_path = await resolve_active_mount(rest_session)
        print(f"Resolved mount: {mount_path}")

        baseline = await stills_marker(rest_session, mount_path)
        print(f"Baseline Stills mtime: {baseline}")

        async with CameraSession(BLE_MODEL_KEY, BLE_FIRMWARE) as ble_session:
            trigger_time = datetime.now()
            try:
                await ble_session.capture_photo()
            except BMDUnsupportedError as exc:
                print(f"capture_photo() not attempted: {exc}")
                return 1
            print("Trigger sent over BLE — no BLE confirmation exists, polling over REST …")

        confirmed = await wait_for_new_still(
            rest_session, mount_path, baseline, timeout_s=CONFIRM_TIMEOUT_S
        )

        if not confirmed:
            print(f"NOT confirmed — Stills directory did not change within {CONFIRM_TIMEOUT_S}s")
            return 1
        print("Confirmed ✓ — Stills directory changed")

        guessed_path = await guess_new_still_path(rest_session, mount_path, around=trigger_time)
        if guessed_path is not None:
            print(f"Likely filename (best-effort guess): {guessed_path}")
        else:
            print(
                "Filename not determined (best-effort guess found nothing in its default "
                "search window) — the capture is still confirmed above."
            )

    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
