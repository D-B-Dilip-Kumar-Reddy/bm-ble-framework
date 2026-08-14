"""
Record and delete one real disposable clip, then call
`RestCameraSession.reconnect()` on the *same* session object and confirm
`clips()`/`storage_state()` are accurate afterward — the first real-hardware
test of `reconnect()` (Phase 14).

WHY THIS EXISTS: `GET /clips/list` can go stale within the same session
immediately after a deletion (real-hardware-confirmed, `POCKET_6K_G2 v8.6`,
2026-08-13/14 — see `delete_clip()`'s own docstring). The only mechanism
confirmed to reliably clear it is a fresh reconnect, but until now that
always meant discarding the whole `RestCameraSession` object and starting a
new script. `reconnect()` tears down and rebuilds the transport on the same
object instead, so a caller never has to swap which reference it holds —
that continuity is the actual thing this script proves, via `id(session)`
before and after.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: records a real `RECORD_SECONDS`
clip (consuming a small amount of real storage and time), then deletes it.
Net effect on the card is nothing, once it succeeds — no typed-confirmation
prompt needed, the same reasoning `rest_delete_clip.py` already relies on:
whatever this script deletes is guaranteed to be a clip it just recorded
itself in this exact run.

SEQUENCE: `record_start()` -> `wait_while_recording(RECORD_SECONDS)` ->
`record_stop()` -> `confirm_new_clip()` -> `delete_clip(confirm=True)` ->
print `clips()` immediately (may or may not be stale — non-fatal either
way, just observational) -> `await session.reconnect()` -> confirm
`id(session)` is unchanged -> print `clips()`/`storage_state()` again
(expected: accurate now, the deleted clip gone and not double-counted).

STATUS: `reconnect()` is new this session, its mechanics traced by hand
against `__aenter__`/`__aexit__`/`RestEventRouter.connect()`'s real
behavior and unit-tested against a fake transport — not yet run against
real hardware. This script's first successful run is that confirmation,
and the first time this codebase will have exercised an in-process,
same-object reconnect at all.

Edit HOST / MODEL_KEY / FIRMWARE / RECORD_SECONDS below to target a
different camera or recording length.

Usage:
    python examples/rest_reconnect_after_delete.py
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

RECORD_SECONDS = 10


async def _print_clips(session: RestCameraSession, label: str) -> int | None:
    print(f"--- {label} ---")
    try:
        clips = await session.clips()
        storage = await session.storage_state()
        count = storage.active_device.clip_count if storage.active_device else None
        print(f"  clips(): {len(clips)}   storage clip_count: {count}")
        return len(clips)
    except BMDStorageError as exc:
        print(f"  {exc}")
        return None


async def main() -> int:
    async with RestCameraSession(HOST, MODEL_KEY, FIRMWARE) as session:
        await _print_clips(session, "BEFORE recording")

        print(f"\n=== Recording for {RECORD_SECONDS}s ===")
        clips_before = await session.clips()
        storage_before = await session.storage_state()
        try:
            await session.record_start()
        except (BMDStorageError, BMDVerificationError) as exc:
            print(f"record_start failed: {exc}")
            return 1
        await session.wait_while_recording(RECORD_SECONDS)
        try:
            await session.record_stop()
        except BMDVerificationError as exc:
            print(f"record_stop failed: {exc}")
            return 1

        try:
            result = await session.confirm_new_clip(clips_before, storage_before=storage_before)
        except BMDVerificationError as exc:
            print(f"confirm_new_clip failed: {exc}")
            return 1
        clip_unique_id = result.clip.clip_unique_id
        print(f"  recorded clip_unique_id={clip_unique_id} ({result.clip.file_path})")

        print(f"\n=== Deleting clip_unique_id={clip_unique_id} ===")
        try:
            await session.delete_clip(clip_unique_id, confirm=True)
        except (ValueError, BMDVerificationError) as exc:
            print(f"delete_clip({clip_unique_id}) NOT confirmed: {exc}")
            return 1
        print(f"delete_clip({clip_unique_id}) confirmed ✓")

        await _print_clips(session, "IMMEDIATELY after deletion (may be stale — informational)")

        session_id_before = id(session)
        print("\n=== Reconnecting on the same session object ===")
        await session.reconnect()
        session_id_after = id(session)
        print(f"  id(session) before: {session_id_before}  after: {session_id_after}")
        if session_id_before != session_id_after:
            print("  UNEXPECTED: id(session) changed — continuity claim violated")
            return 1
        print("  id(session) unchanged ✓ — same object, fresh connection")

        await _print_clips(session, "AFTER reconnect (expected: accurate)")

        try:
            clips_after = await session.clips()
        except BMDStorageError as exc:
            print(f"clips() after reconnect failed: {exc}")
            return 1
        still_listed = any(clip.clip_unique_id == clip_unique_id for clip in clips_after)
        if still_listed:
            print(f"  clip_unique_id={clip_unique_id} STILL listed after reconnect — unexpected")
            return 1
        print(f"  clip_unique_id={clip_unique_id} confirmed gone after reconnect ✓")

    return 0


if __name__ == "__main__":
    # Redirected/piped stdout is fully buffered by default in CPython, while
    # logging's StreamHandler flushes per record — without this, captured
    # log files interleave print() and logging output out of chronological
    # order, making timing hard to trust when diagnosing issues later.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
