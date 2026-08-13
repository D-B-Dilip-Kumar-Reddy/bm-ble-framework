"""
Permanently delete one clip from the active storage device — entirely over
REST, no BLE involved. `RestCameraSession.delete_clip()` (Phase 11) is the
capability `tools/rest/probe_endpoints.py`'s `--probe-mounts-delete`/
`--delete-real-file` investigation exists to answer — see
`docs/rest/transport.md`'s Mode 3 section for the full evidentiary trail and
`delete_clip()`'s own docstring for exactly what is and isn't confirmed.

**THIS PERMANENTLY ERASES THE CLIP, IRREVERSIBLY.** Because of that, this
script layers a safety gate on top of `delete_clip()`'s own mandatory
`confirm=True` argument, matching `examples/rest_format_device.py`'s
convention rather than every other examples/ script's plain "edit the
constants and run it" shape:

  1. Prints the full `clips()` inventory first, so the operator can see
     exactly what's on the card and pick a real `clip_unique_id` from it.
  2. Requires typing the *exact* clip filename back at a prompt — not just
     "yes" — before `delete_clip()` is ever called. Ctrl-C or any other
     input aborts with nothing sent to the camera.

STATUS: `delete_clip()`'s underlying `GET`/`DELETE`/`GET` sequence is
real-hardware-confirmed (`POCKET_6K_G2 v8.6`, 2026-08-13, done by hand in
Postman after `tools/rest/probe_endpoints.py`'s own attempt crashed on a
binary-body bug — since fixed). This script, and `delete_clip()` composed
through `RestCameraSession`'s own machinery, has not itself been run
against real hardware yet — this script's first successful run *is* that
confirmation.

**Only clip deletion is confirmed.** No still's exact `/mounts/...` path
has been independently confirmed the way this clip's was (the Stills
directory's own known `500` listing defect means one can't be read off a
listing) — there is no `delete_still()` yet, and this script only ever
targets clips from `clips()`.

Edit HOST / MODEL_KEY / FIRMWARE below to target a different camera.
CLIP_UNIQUE_ID is left unset on purpose — the printed inventory below is
where a real id comes from, never a guess.

Usage:
    python examples/rest_delete_clip.py
"""

import asyncio
import logging
import sys

from bmd_camera import BMDVerificationError, RestCameraSession
from bmd_camera.exceptions import BMDStorageError

HOST = "pocket-cinema-camera-6k-pro.local"
MODEL_KEY = "POCKET_6K_PRO"
FIRMWARE = "v8.6"
# HOST = "pocket-cinema-camera-6k-g2.local"
# MODEL_KEY = "POCKET_6K_G2"
# FIRMWARE = "v8.6"

# Required — must be a real clip_unique_id from the printed clips() inventory
# below. Left None here on purpose, so a caller who hasn't looked at that
# printout yet gets a clear abort rather than an unverified guess sent to
# the camera.
CLIP_UNIQUE_ID: int | None = None


async def _print_clips(session: RestCameraSession):
    try:
        clips = await session.clips()
    except BMDStorageError as exc:
        print(f"  {exc}")
        return ()
    if not clips:
        print("  No clips on card.")
        return clips
    for clip in clips:
        print(
            f"  clip_unique_id={clip.clip_unique_id}  {clip.file_path}  "
            f"({clip.duration_timecode}, {clip.codec}, {clip.video_format})"
        )
    return clips


def _confirm_by_typing_filename(filename: str) -> bool:
    print(f"\nThis will PERMANENTLY DELETE {filename!r} from {HOST}. This cannot be undone.")
    answer = input(f"Type the file name ({filename!r}) to proceed, anything else aborts: ")
    return answer == filename


async def main() -> int:
    async with RestCameraSession(HOST, MODEL_KEY, FIRMWARE) as session:
        print("--- Clips before deletion ---")
        clips = await _print_clips(session)

        if CLIP_UNIQUE_ID is None:
            print(
                "\nCLIP_UNIQUE_ID is not set. Edit this script and set it to one of the "
                "clip_unique_id values printed above, then run it again. Nothing was sent "
                "to the camera."
            )
            return 1

        target_clip = next((c for c in clips if c.clip_unique_id == CLIP_UNIQUE_ID), None)
        if target_clip is None:
            print(f"\nclip_unique_id={CLIP_UNIQUE_ID} is not in the printout above.")
            return 1
        filename = target_clip.file_path.rsplit("/", 1)[-1]

        if not _confirm_by_typing_filename(filename):
            print("Aborted — file name did not match, nothing was sent to the camera.")
            return 1

        print(f"\n=== Deleting clip_unique_id={CLIP_UNIQUE_ID} ({filename}) ===")
        try:
            deleted = await session.delete_clip(CLIP_UNIQUE_ID, confirm=True)
        except (ValueError, BMDVerificationError) as exc:
            print(f"delete_clip({CLIP_UNIQUE_ID}) NOT confirmed: {exc}")
            return 1
        print(f"delete_clip({CLIP_UNIQUE_ID}) confirmed ✓ — {deleted.file_path} is gone")

        print("\n--- Clips after deletion ---")
        await _print_clips(session)

    return 0


if __name__ == "__main__":
    # Redirected/piped stdout is fully buffered by default in CPython, while
    # logging's StreamHandler flushes per record — without this, captured
    # log files interleave print() and logging output out of chronological
    # order, making timing hard to trust when diagnosing issues later.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
