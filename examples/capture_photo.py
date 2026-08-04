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
`rest/media.py` supplies the confirmation BLE structurally cannot: watch
for a new still file to appear on the SD card, over REST. This script is
therefore the first thing in this codebase to hold a BLE `CameraSession`
and a REST `RestCameraSession` open to the *same physical camera*
simultaneously — an untested combination (the plan's own risk list flags
"concurrent BLE + REST is unverified... Phase 6 needs both open at once —
confirm on hardware"). This run is that confirmation.

WHICH CAMERA/FIRMWARE
-------------------------
The photo trigger is only confirmed in the profile for `POCKET_6K_PRO v8.6`
and `POCKET_6K_G2 v7.9` — **not** `POCKET_6K_G2 v8.6`, this codebase's usual
primary reference, which has no `photo` command block yet (needs
`tools/control/discover_command.py --data-type VOID` run against it first;
see `CameraSession.capture_photo()`'s docstring). Defaults to
`POCKET_6K_PRO v8.6` for this reason — BLE_MODEL_KEY/BLE_FIRMWARE and
HOST/REST_MODEL_KEY/REST_FIRMWARE all target the same physical camera, just
over two different transports.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: takes one real photo.

CONFIRMATION DESIGN — pieces still not independently re-confirmed in this
codebase
------------------------------------------------------------------------------
`rest/media.py`'s filename-prefix derivation (a still shares a clip's
`<reel>_<date>` stem) and the mount-path resolution are both explained in
that module's docstring, including which parts are confirmed on real
hardware and which are inherited from the original plan, not yet
independently re-confirmed by a tool in this codebase. This script's own
run is what confirms or refutes them for real.

Usage:
    python examples/capture_photo.py
"""

import asyncio
import logging
import sys

from bmd_camera import BMDUnsupportedError, CameraSession, RestCameraSession
from bmd_camera.exceptions import BMDStorageError
from bmd_camera.rest.media import (
    derive_still_prefix,
    find_highest_still_index,
    resolve_active_mount,
    wait_for_new_still,
)

HOST = "pocket-cinema-camera-6k-pro.local"
REST_MODEL_KEY = "POCKET_6K_PRO"
REST_FIRMWARE = "v8.6"
BLE_MODEL_KEY = "POCKET_6K_PRO"
BLE_FIRMWARE = "v8.6"
# HOST = "pocket-cinema-camera-6k-g2.local"
# REST_MODEL_KEY = "POCKET_6K_G2"
# REST_FIRMWARE = "v8.6"
# BLE_MODEL_KEY = "POCKET_6K_G2"
# BLE_FIRMWARE = "v7.9"  # v8.6 has no confirmed photo trigger yet

CONFIRM_TIMEOUT_S = 15.0


async def main() -> int:
    async with RestCameraSession(HOST, REST_MODEL_KEY, REST_FIRMWARE) as rest_session:
        storage = await rest_session.storage_state()
        if storage.active_device is None:
            raise BMDStorageError(f"[{HOST}] No active storage device — cannot capture a photo")
        print(f"Active storage: {storage.active_device.device_name}")

        try:
            clips = await rest_session.clips()
        except BMDStorageError as exc:
            print(f"clips(): {exc}")
            clips = ()
        if not clips:
            print(
                "No clips on the card yet — cannot derive a still filename prefix "
                "(rest/media.py's derive_still_prefix() needs an existing clip's "
                "filePath). Record at least one short clip first."
            )
            return 1
        prefix = derive_still_prefix(clips[-1].file_path)
        print(f"Still filename prefix: {prefix}")

        mount_path = await resolve_active_mount(rest_session)
        print(f"Resolved mount: {mount_path}")

        baseline = await find_highest_still_index(rest_session, mount_path, prefix)
        print(f"Baseline highest still index: {baseline}")

        async with CameraSession(BLE_MODEL_KEY, BLE_FIRMWARE) as ble_session:
            try:
                await ble_session.capture_photo()
            except BMDUnsupportedError as exc:
                print(f"capture_photo() not attempted: {exc}")
                return 1
            print("Trigger sent over BLE — no BLE confirmation exists, polling over REST …")

        confirmed_index = await wait_for_new_still(
            rest_session, mount_path, prefix, baseline, timeout_s=CONFIRM_TIMEOUT_S
        )

    if confirmed_index is None:
        print(f"NOT confirmed — no new still appeared within {CONFIRM_TIMEOUT_S}s")
        return 1
    print(f"Confirmed ✓ — new still at index {confirmed_index}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
