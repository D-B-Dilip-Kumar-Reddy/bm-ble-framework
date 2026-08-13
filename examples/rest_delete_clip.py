"""
Record a real 10-second clip, then permanently delete it — a self-contained
round-trip real-hardware test of `RestCameraSession.delete_clip()` (Phase
11). Recording its own disposable clip rather than asking the operator to
pick one from the card's existing footage means this script needs no
typed-confirmation prompt: whatever it deletes is guaranteed to be
something it just created itself in this exact run, never irreplaceable
existing footage — the same reasoning `examples/rest_record_test_clip.py`
already relies on to record real clips with no interactive gate.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: records a real `RECORD_SECONDS`
clip (consuming a small amount of real storage and time), then deletes
that exact clip. Net effect on the card is nothing, once it succeeds —
unlike `examples/rest_format_device.py`/`rest_delete_clip.py`'s earlier
version, there is no existing data at risk here by construction.

SEQUENCE: `record_start()` -> `wait_while_recording(RECORD_SECONDS)` ->
`record_stop()` -> `confirm_new_clip()` (Phase 9 — the before/after
`clips()` diff that names the just-written clip; `GET /clips/list` has no
"just-written" flag of its own) -> `delete_clip(confirm=True)` (Phase
11) -> a final `clips()` printout to show it's actually gone. Modeled
directly on `rest_record_test_clip.py`'s recording step and state-
printing shape.

STATUS: `delete_clip()`'s underlying `GET`/`DELETE`/`GET` sequence is
real-hardware-confirmed (`POCKET_6K_G2 v8.6`, 2026-08-13, done by hand in
Postman after `tools/rest/probe_endpoints.py`'s own attempt crashed on a
binary-body bug — since fixed). This script, and `delete_clip()` composed
through `RestCameraSession`'s own machinery end to end, has not itself
been run against real hardware yet — this script's first successful run
*is* that confirmation. See `delete_clip()`'s own docstring
(`docs/rest/session.md`'s Phase 11 section) for exactly what is and isn't
confirmed, including the single-sample path-construction caveat.

**Only clip deletion is confirmed.** No still's exact `/mounts/...` path
has been independently confirmed the way this clip's is about to be (the
Stills directory's own known `500` listing defect means one can't be read
off a listing) — there is no `delete_still()`.

Edit HOST / MODEL_KEY / FIRMWARE / RECORD_SECONDS below to target a
different camera or recording length.

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

RECORD_SECONDS = 10


async def _print_clips(session: RestCameraSession, label: str):
    print(f"--- {label}: clip inventory ---")
    try:
        clips = await session.clips()
        print(f"  {len(clips)} clip(s) on card")
    except BMDStorageError as exc:
        print(f"  {exc}")
        clips = ()
    return clips


async def main() -> int:
    async with RestCameraSession(HOST, MODEL_KEY, FIRMWARE) as session:
        clips_before = await _print_clips(session, "BEFORE recording")
        storage_before = await session.storage_state()

        print(f"\n=== Recording for {RECORD_SECONDS}s ===")
        try:
            await session.record_start()
        except (BMDStorageError, BMDVerificationError) as exc:
            print(f"record_start failed: {exc}")
            return 1
        print("record_start confirmed ✓")

        held = await session.wait_while_recording(RECORD_SECONDS)
        if not held:
            print(f"Recording stopped before the requested {RECORD_SECONDS}s")

        try:
            await session.record_stop()
        except BMDVerificationError as exc:
            print(f"record_stop failed: {exc}")
            return 1
        print("record_stop confirmed ✓")

        await _print_clips(session, "AFTER recording")

        print("\n=== Identifying the new clip ===")
        try:
            result = await session.confirm_new_clip(clips_before, storage_before=storage_before)
        except BMDVerificationError as exc:
            print(f"confirm_new_clip failed: {exc}")
            return 1

        clip = result.clip
        print(f"  clip_unique_id: {clip.clip_unique_id}")
        print(f"  name:           {clip.file_path}")
        print(f"  length:         {clip.duration_timecode}")

        print(f"\n=== Deleting clip_unique_id={clip.clip_unique_id} ({clip.file_path}) ===")
        try:
            deleted = await session.delete_clip(clip.clip_unique_id, confirm=True)
        except (ValueError, BMDVerificationError) as exc:
            print(f"delete_clip({clip.clip_unique_id}) NOT confirmed: {exc}")
            return 1
        print(f"delete_clip({clip.clip_unique_id}) confirmed ✓ — {deleted.file_path} is gone")

        await _print_clips(session, "AFTER deletion")

    return 0


if __name__ == "__main__":
    # Redirected/piped stdout is fully buffered by default in CPython, while
    # logging's StreamHandler flushes per record — without this, captured
    # log files interleave print() and logging output out of chronological
    # order, making timing hard to trust when diagnosing issues later.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
