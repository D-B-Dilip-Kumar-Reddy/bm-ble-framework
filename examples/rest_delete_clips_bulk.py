"""
Record several real short clips, then permanently delete all of them in a
single batch — a self-contained real-hardware test of
`RestCameraSession.delete_clips()` (Phase 13). Recording its own
disposable clips rather than asking the operator to pick some from the
card's existing footage means this script needs no typed-confirmation
prompt, the same reasoning `examples/rest_delete_clip.py` already relies
on for a single clip.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: records `CLIP_COUNT` real
`RECORD_SECONDS` clips (consuming a small amount of real storage and
time), then deletes exactly those clips in one `delete_clips()` call. Net
effect on the card is nothing, once it succeeds.

SEQUENCE: record `CLIP_COUNT` clips one at a time (`record_start()` ->
`wait_while_recording(RECORD_SECONDS)` -> `record_stop()` ->
`confirm_new_clip()` to name each one, exactly like
`rest_delete_clip.py`'s single-clip version) -> `delete_clips(all_ids,
confirm=True)` -> a final `clips()` printout showing the batch is gone.

STATUS: `delete_clips()` is new this session, built entirely on
`delete_clip()`'s already-real-hardware-confirmed `GET`/`DELETE`/`GET`
sequence — this script's first successful run is delete_clips()'s own
first real-hardware confirmation, the same status every capability in
this codebase carries before its first real-hardware pass.

Edit HOST / MODEL_KEY / FIRMWARE / CLIP_COUNT / RECORD_SECONDS below to
target a different camera, batch size, or recording length.

Usage:
    python examples/rest_delete_clips_bulk.py
"""

import asyncio
import logging
import sys

from bmd_camera import BMDVerificationError, RestCameraSession
from bmd_camera.exceptions import BMDStorageError

HOST = "pocket-cinema-camera-6k-g2.local"
MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v8.6"
# HOST = "pocket-cinema-camera-6k-pro.local"
# MODEL_KEY = "POCKET_6K_PRO"
# FIRMWARE = "v8.6"

CLIP_COUNT = 3
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


async def _record_one_clip(session: RestCameraSession, index: int) -> int | None:
    print(f"\n=== Recording clip {index}/{CLIP_COUNT} ({RECORD_SECONDS}s) ===")
    clips_before = await session.clips()
    storage_before = await session.storage_state()

    try:
        await session.record_start()
    except (BMDStorageError, BMDVerificationError) as exc:
        print(f"record_start failed: {exc}")
        return None
    await session.wait_while_recording(RECORD_SECONDS)
    try:
        await session.record_stop()
    except BMDVerificationError as exc:
        print(f"record_stop failed: {exc}")
        return None

    try:
        result = await session.confirm_new_clip(clips_before, storage_before=storage_before)
    except BMDVerificationError as exc:
        print(f"confirm_new_clip failed: {exc}")
        return None

    print(f"  recorded clip_unique_id={result.clip.clip_unique_id} ({result.clip.file_path})")
    return result.clip.clip_unique_id


async def main() -> int:
    async with RestCameraSession(HOST, MODEL_KEY, FIRMWARE) as session:
        await _print_clips(session, "BEFORE recording")

        clip_unique_ids: list[int] = []
        for i in range(1, CLIP_COUNT + 1):
            clip_unique_id = await _record_one_clip(session, i)
            if clip_unique_id is None:
                print("Stopping — a recording step failed.")
                return 1
            clip_unique_ids.append(clip_unique_id)

        await _print_clips(session, "AFTER recording")

        print(f"\n=== Bulk-deleting {clip_unique_ids} ===")
        result = await session.delete_clips(clip_unique_ids, confirm=True)

        print(f"  deleted: {[c.clip_unique_id for c in result.deleted]}")
        if result.failed:
            print(f"  FAILED: {[(cid, str(exc)) for cid, exc in result.failed]}")
        else:
            print("  all clips confirmed deleted ✓")

        await _print_clips(session, "AFTER bulk deletion")

    return 1 if result.failed else 0


if __name__ == "__main__":
    # Redirected/piped stdout is fully buffered by default in CPython, while
    # logging's StreamHandler flushes per record — without this, captured
    # log files interleave print() and logging output out of chronological
    # order, making timing hard to trust when diagnosing issues later.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
