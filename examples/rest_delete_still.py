"""
Trigger a still photo capture over BLE, confirm it over REST, guess its
real filename, then permanently delete it — a self-contained round-trip
real-hardware test of `RestCameraSession.delete_still()` (Phase 11),
mirroring `examples/capture_photo.py`'s BLE+REST composition (see that
script's own docstring for why both transports are open at once) rather
than `examples/rest_delete_clip.py`'s REST-only shape — there is no REST
way to trigger a photo capture at all, only to confirm and now delete one.

WHY THIS NEEDS A FILENAME GUESS, UNLIKE `rest_delete_clip.py`
-------------------------------------------------------------
`delete_clip()` resolves `clip_unique_id` against a working `clips()`
listing. Stills have no such listing — the Stills directory itself `500`s
unconditionally (`rest/media.py`'s module docstring) — so there is no
id-based, guess-free way to identify *which* still to delete. This script
uses the same opt-in, best-effort `guess_new_still_path()` that
`capture_photo.py` already uses purely informationally, but here the
guess is load-bearing: if it returns `None`, there is nothing to delete
and this script stops rather than inventing a path. `delete_still()`
itself never guesses on its own (design principle 7) — it only accepts a
path a caller already has.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: takes one real photo, then
deletes it. Net effect on the card is nothing, once it succeeds.

STATUS: `delete_still()`'s underlying `GET`/`DELETE`/`GET` sequence is
real-hardware-confirmed (`POCKET_6K_G2 v8.6`, 2026-08-13, done by hand in
Postman on `/mounts/A002-sd1/Stills/A002_08120219_S001.braw`). This
script, and `delete_still()` composed through `RestCameraSession`'s own
machinery end to end, has not itself been run against real hardware yet
— this script's first successful run *is* that confirmation.

Usage:
    python examples/rest_delete_still.py
"""

import asyncio
import logging
import sys
from datetime import datetime

from bmd_camera import BMDUnsupportedError, BMDVerificationError, CameraSession, RestCameraSession
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
        print("Capture confirmed ✓ — Stills directory changed")

        print("\n=== Guessing the still's real filename ===")
        guessed_path = await guess_new_still_path(rest_session, mount_path, around=trigger_time)
        if guessed_path is None:
            print(
                "Could not guess a filename — the capture is still confirmed above, but "
                "there is nothing to delete without a real path. delete_still() never "
                "guesses on its own. Nothing was sent to the camera."
            )
            return 1
        print(f"  {guessed_path}")

        print(f"\n=== Deleting {guessed_path} ===")
        try:
            await rest_session.delete_still(guessed_path, confirm=True)
        except (ValueError, BMDVerificationError) as exc:
            print(f"delete_still({guessed_path!r}) NOT confirmed: {exc}")
            return 1
        print(f"delete_still({guessed_path!r}) confirmed ✓ — gone")

    return 0


if __name__ == "__main__":
    # Redirected/piped stdout is fully buffered by default in CPython, while
    # logging's StreamHandler flushes per record — without this, captured
    # log files interleave print() and logging output out of chronological
    # order, making timing hard to trust when diagnosing issues later.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
