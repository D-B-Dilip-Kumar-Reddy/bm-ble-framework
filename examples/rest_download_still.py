"""
Trigger a still photo capture over BLE, confirm it over REST, guess its
real filename, then download it to the local PC —
`RestCameraSession.download_still()` (Phase 12). Mirrors
`examples/capture_photo.py`'s BLE-trigger + REST-confirm composition
(there is no REST way to trigger a photo at all) and
`examples/rest_delete_still.py`'s guess-then-act shape, swapping the final
destructive `delete_still()` for a non-destructive `download_still()`.

WHY THIS NEEDS A FILENAME GUESS, UNLIKE `rest_download_clip.py`
-----------------------------------------------------------------
`download_clip()` resolves `clip_unique_id` against a working `clips()`
listing. Stills have no such listing — the Stills directory itself `500`s
unconditionally (`rest/media.py`'s module docstring) — so there is no
id-based, guess-free way to identify *which* still to download. This
script uses the same opt-in, best-effort `guess_new_still_path()` that
`capture_photo.py` uses purely informationally, but here the guess is
load-bearing: if it returns `None`, there is nothing to download and this
script stops rather than inventing a path. **A real, confirmed failure
mode of this guess**: if the camera's own onboard clock has drifted from
the operator's PC clock, the guess's narrow search window can miss
entirely — see `rest/media.py`'s module docstring ("CAMERA CLOCK SKEW")
for the real-hardware finding this was built from. If that happens here,
obtain the real filename by another means (as that finding's own
follow-up run did) and call `RestCameraSession.download_still()` directly
with the real path instead of relying on this script's guess.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: takes one real photo. Nothing is
deleted — unlike `rest_delete_still.py`, this is non-destructive; the
photo stays on the card.

STATUS: `RestClient.download()`'s streaming/`Content-Length`-integrity
logic and `download_still()` are both new this session, unit-tested
against a fake client only — not yet run against real hardware. This
script's first successful run is that confirmation.

Usage:
    python examples/rest_download_still.py
"""

import asyncio
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

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
DEST_DIR = Path(__file__).parent / "downloads"


def _format_bytes(num_bytes: int) -> str:
    gb = num_bytes / 1_000_000_000
    return f"{num_bytes} bytes ({gb:.2f} GB)"


async def main() -> int:
    DEST_DIR.mkdir(parents=True, exist_ok=True)

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
                "there is nothing to download without a real path. download_still() never "
                "guesses on its own. If the camera's clock is known to be off, obtain the "
                "real filename by another means and call download_still() with it directly."
            )
            return 1
        print(f"  {guessed_path}")

        print(f"\n=== Downloading {guessed_path} -> {DEST_DIR} ===")
        start = time.monotonic()
        try:
            dest = await rest_session.download_still(guessed_path, DEST_DIR, overwrite=True)
        except (ValueError, FileExistsError, BMDVerificationError) as exc:
            print(f"download_still({guessed_path!r}) failed: {exc}")
            return 1
        elapsed = time.monotonic() - start

        size = dest.stat().st_size
        rate_mb_s = (size / 1_000_000) / elapsed if elapsed > 0 else 0.0
        print(f"download_still({guessed_path!r}) confirmed ✓")
        print(f"  {dest}")
        print(f"  {_format_bytes(size)} in {elapsed:.1f}s ({rate_mb_s:.1f} MB/s)")

    return 0


if __name__ == "__main__":
    # Redirected/piped stdout is fully buffered by default in CPython, while
    # logging's StreamHandler flushes per record — without this, captured
    # log files interleave print() and logging output out of chronological
    # order, making timing hard to trust when diagnosing issues later.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
