"""
Capture several stills in one session, each guessed, downloaded, and then
deleted — the first multi-still workflow in this codebase, and the
real-hardware exercise of `guess_new_still_path()`'s `exclude` parameter
(added 2026-08-24 specifically for this scenario).

WHY THIS NEEDS `exclude`, UNLIKE EVERY EARLIER SINGLE-STILL SCRIPT
--------------------------------------------------------------------
`capture_photo.py`/`rest_delete_still.py`/`rest_download_still.py` all
guess exactly one still per run, so there is never a previous guess to
collide with. `guess_new_still_path()`'s default `minute_offsets` was
widened to `(0, -1, 1, -2, 2, -3, 3)` (docs/rest/session.md,
`rest/media.py`'s module docstring) to cover the fact that the SETUP >
Date/Time screen has no Seconds field — but that wider ±3-minute search
radius creates a new risk unique to *repeated* captures: an **earlier**
still's real, still-existing filename can sit well within a **later**
still's search window and get matched first, silently returning a stale,
already-processed path instead of the new one. This script accumulates
every path it has already guessed into `guessed_paths` and passes it as
`exclude` on each subsequent guess, so a stale match is skipped rather
than returned — this run is `exclude`'s first real-hardware exercise.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: takes `STILL_COUNT` real photos,
downloads each to `DEST_DIR`, then deletes each from the card. Net effect
on the card is nothing once a still's round trip succeeds (matching
`rest_delete_still.py`'s own framing) — a local copy of each still is kept.

PER-STILL SEQUENCE (mirrors `rest_delete_still.py` and
`rest_download_still.py`'s guess-then-act shape, run once per still):
snapshot the Stills `mtime` baseline -> trigger over BLE (fresh
`CameraSession`, matching every earlier photo-capture script's connect/
disconnect-per-trigger pattern) -> confirm over REST
(`wait_for_new_still()`) -> guess the filename (`guess_new_still_path()`,
`exclude=guessed_paths`) -> download it -> delete it.

PARTIAL FAILURE IS EXPECTED, NOT FATAL — one still's failure at any step
does not stop the batch, the same partial-success philosophy
`delete_clips()`/`download_clips()` already established for their own bulk
operations (`docs/rest/session.md`). Each still's outcome (captured,
guessed, downloaded, deleted, or where it stopped and why) is tracked and
printed in a final summary — this is why the script is not just three
single-still scripts pasted in a loop.

STATUS: `exclude` is new this session, unit-tested against a fake client
only — not yet run against real hardware. This script's first successful
run is that confirmation, alongside the whole guess+download+delete
composition it exercises for the first time as a repeated sequence.

Usage:
    python examples/capture_multiple_stills.py
"""

import asyncio
import logging
import sys
import time
from dataclasses import dataclass
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

STILL_COUNT = 3
CONFIRM_TIMEOUT_S = 15.0
DEST_DIR = Path(__file__).parent / "downloads"


def _format_bytes(num_bytes: int) -> str:
    gb = num_bytes / 1_000_000_000
    return f"{num_bytes} bytes ({gb:.2f} GB)"


@dataclass
class StillOutcome:
    index: int
    captured: bool = False
    guessed_path: str | None = None
    downloaded_to: Path | None = None
    deleted: bool = False
    stopped_at: str | None = None
    error: str = ""

    def summary_line(self) -> str:
        if self.deleted:
            return f"  [{self.index}] OK — {self.guessed_path}"
        stage = self.stopped_at or "unknown"
        return f"  [{self.index}] FAILED at {stage} — {self.error}"


async def capture_one_still(
    rest_session: RestCameraSession,
    mount_path: str,
    index: int,
    guessed_paths: list[str],
) -> StillOutcome:
    outcome = StillOutcome(index=index)

    baseline = await stills_marker(rest_session, mount_path)

    async with CameraSession(BLE_MODEL_KEY, BLE_FIRMWARE) as ble_session:
        trigger_time = datetime.now()
        try:
            await ble_session.capture_photo()
        except BMDUnsupportedError as exc:
            outcome.stopped_at = "trigger"
            outcome.error = str(exc)
            return outcome
    print(f"  [{index}] Trigger sent over BLE — confirming over REST …")

    confirmed = await wait_for_new_still(
        rest_session, mount_path, baseline, timeout_s=CONFIRM_TIMEOUT_S
    )
    if not confirmed:
        outcome.stopped_at = "confirm"
        outcome.error = f"Stills directory did not change within {CONFIRM_TIMEOUT_S}s"
        return outcome
    outcome.captured = True
    print(f"  [{index}] Confirmed ✓")

    guessed_path = await guess_new_still_path(
        rest_session, mount_path, around=trigger_time, exclude=guessed_paths
    )
    if guessed_path is None:
        outcome.stopped_at = "guess"
        outcome.error = "guess_new_still_path() found nothing new in its default search window"
        return outcome
    outcome.guessed_path = guessed_path
    guessed_paths.append(guessed_path)
    print(f"  [{index}] Guessed: {guessed_path}")

    try:
        dest = await rest_session.download_still(guessed_path, DEST_DIR, overwrite=True)
    except (ValueError, FileExistsError, BMDVerificationError) as exc:
        outcome.stopped_at = "download"
        outcome.error = str(exc)
        return outcome
    outcome.downloaded_to = dest
    size = dest.stat().st_size
    print(f"  [{index}] Downloaded: {dest} ({_format_bytes(size)})")

    try:
        await rest_session.delete_still(guessed_path, confirm=True)
    except (ValueError, BMDVerificationError) as exc:
        outcome.stopped_at = "delete"
        outcome.error = str(exc)
        return outcome
    outcome.deleted = True
    print(f"  [{index}] Deleted ✓")

    return outcome


async def main() -> int:
    DEST_DIR.mkdir(parents=True, exist_ok=True)

    async with RestCameraSession(HOST, REST_MODEL_KEY, REST_FIRMWARE) as rest_session:
        storage = await rest_session.storage_state()
        if storage.active_device is None:
            raise BMDStorageError(f"[{HOST}] No active storage device — cannot capture a photo")
        print(f"Active storage: {storage.active_device.device_name}")

        mount_path = await resolve_active_mount(rest_session)
        print(f"Resolved mount: {mount_path}")

        guessed_paths: list[str] = []
        outcomes: list[StillOutcome] = []
        start = time.monotonic()

        for index in range(1, STILL_COUNT + 1):
            print(f"\n=== Still {index}/{STILL_COUNT} ===")
            outcome = await capture_one_still(rest_session, mount_path, index, guessed_paths)
            outcomes.append(outcome)
            if not outcome.deleted:
                print(f"  [{index}] Stopped at {outcome.stopped_at}: {outcome.error}")

        elapsed = time.monotonic() - start

    print(
        f"\n=== Summary ({sum(o.deleted for o in outcomes)}/{STILL_COUNT} succeeded, "
        f"{elapsed:.1f}s total) ==="
    )
    for outcome in outcomes:
        print(outcome.summary_line())
    if len({o.guessed_path for o in outcomes if o.guessed_path}) != len(
        [o for o in outcomes if o.guessed_path]
    ):
        print(
            "\nWARNING: two stills guessed the SAME path — exclude did not prevent a "
            "collision. This would be a real defect; report it rather than trusting the "
            "run above."
        )

    return 0 if all(o.deleted for o in outcomes) else 1


if __name__ == "__main__":
    # Redirected/piped stdout is fully buffered by default in CPython, while
    # logging's StreamHandler flushes per record — without this, captured
    # log files interleave print() and logging output out of chronological
    # order, making timing hard to trust when diagnosing issues later.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
