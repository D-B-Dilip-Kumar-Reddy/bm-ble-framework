"""
tools/rest/verify_confirm_new_clip_edge_cases.py
====================================================
Real-hardware verification for `RestCameraSession.confirm_new_clip()`'s
defensive branches (Phase 9, `docs/rest/session.md`'s `confirm_new_clip()`
section) — the ones PR #17 explicitly named as deferred: the zero-new-clip
and more-than-one-new-clip guards, and `RecordingResult.bytes_written`'s
`None`-returning cases. All five are already covered by `TestConfirmNewClip`
against a fake client (`tests/unit/rest/test_rest_session.py`) but, until
this tool's first run, never against a real camera.

WHAT THIS TOOL DOES — four tests, in order
--------------------------------------------
1. **Zero new clips.** No recording. Snapshots `clips()`, then calls
   `confirm_new_clip()` against that exact snapshot with nothing recorded in
   between — guaranteed zero new clips. Cheapest and safest test here: no
   camera state changes at all.
2. **`storage_before` omitted.** Records one short disposable clip, then
   calls `confirm_new_clip(clips_before)` with no `storage_before` argument
   — `bytes_written` must be `None`.
3. **`storage_before.active_device is None`.** Same recorded clip as test 2,
   called again against the same unchanged snapshot (idempotent — no new
   camera-side effect), this time passing a hand-built
   `StorageState(devices=(), active_device=None)` as `storage_before`.
   `confirm_new_clip()` only inspects the *shape* of whatever `StorageState`
   it's given — it never re-validates that it was freshly fetched — so this
   is a legitimate way to exercise this exact guard against a real, live
   `clips()` diff, deliberately, rather than by chance.
4. **More than one new clip.** Records two more short disposable clips
   back-to-back, sharing a single before-snapshot taken before either one.
   Calls `confirm_new_clip()` against that one shared snapshot on purpose —
   this is a deliberate misuse of the method (an intentionally stale/broad
   snapshot), not a hardware quirk, and proves the "never guess which one is
   the recording" guard actually fires.

**Deliberately not attempted**: the fifth defensive branch —
`bytes_written` staying `None` because the *fresh* `storage_after` (the
live second `storage_state()` call `confirm_new_clip()` makes internally)
reports no active device. Forcing this would need the SD card to genuinely
report no active device at the exact moment right after a clip was just
written — pulling the card to simulate it would very likely make the
*first* `clips()` call (which runs before this branch is ever reached) fail
with a 404 -> `BMDStorageError` instead, never reaching this branch at all.
A disruptive, uncertain-outcome physical action for a single low-value
branch — left accepted and documented as real-hardware-unexercised, the
same honest treatment this codebase already gives a few other
structurally-hard-to-reach branches.

WHAT THIS CHANGES ON THE CAMERA: records and deletes 3 short disposable
clips (`RECORD_SECONDS` each). Net effect on the card is nothing once it
succeeds — no typed-confirmation prompt needed, the same reasoning
`rest_delete_clip.py`/`rest_delete_clips_bulk.py` already rely on: every
clip this tool ever touches is one it recorded itself in this exact run.

USAGE
-----
    python tools/rest/verify_confirm_new_clip_edge_cases.py \\
        --host pocket-cinema-camera-6k-g2.local \\
        --model-key POCKET_6K_G2 --firmware v8.6
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from bmd_camera import BMDVerificationError, RestCameraSession
from bmd_camera.exceptions import BMDStorageError
from bmd_camera.rest.state import StorageState

logger = logging.getLogger(__name__)

RECORD_SECONDS = 5


async def test_zero_new_clips(session: RestCameraSession) -> bool:
    print("\n=== Test 1: zero new clips (no recording) ===")
    snapshot = await session.clips()
    try:
        await session.confirm_new_clip(snapshot)
    except BMDVerificationError as exc:
        if "no new clip" in str(exc):
            print(f"  PASS — raised as expected: {exc}")
            return True
        print(f"  FAIL — raised, but not the expected message: {exc}")
        return False
    print("  FAIL — did not raise")
    return False


async def test_storage_before_omitted_and_no_active_device(session: RestCameraSession) -> bool:
    print("\n=== Tests 2 & 3: bytes_written=None (storage_before omitted / no active device) ===")
    clips_before = await session.clips()
    await session.record_start()
    await session.wait_while_recording(RECORD_SECONDS)
    await session.record_stop()

    ok = True

    result = await session.confirm_new_clip(clips_before)
    clip_unique_id = result.clip.clip_unique_id
    print(f"  recorded clip_unique_id={clip_unique_id}")
    if result.bytes_written is None:
        print("  Test 2 PASS — bytes_written is None when storage_before omitted")
    else:
        print(f"  Test 2 FAIL — expected None, got {result.bytes_written}")
        ok = False

    fake_storage_before = StorageState(devices=(), active_device=None)
    result2 = await session.confirm_new_clip(clips_before, storage_before=fake_storage_before)
    if result2.clip.clip_unique_id != clip_unique_id:
        print(
            f"  Test 3 FAIL — expected the same clip_unique_id={clip_unique_id} again, "
            f"got {result2.clip.clip_unique_id}"
        )
        ok = False
    elif result2.bytes_written is None:
        print("  Test 3 PASS — bytes_written is None when storage_before.active_device is None")
    else:
        print(f"  Test 3 FAIL — expected None, got {result2.bytes_written}")
        ok = False

    print(f"\n=== Cleaning up clip_unique_id={clip_unique_id} ===")
    await session.delete_clip(clip_unique_id, confirm=True)
    print("  deleted ✓")

    return ok


async def test_more_than_one_new_clip(session: RestCameraSession) -> bool:
    print("\n=== Test 4: more than one new clip ===")
    clips_before = await session.clips()

    await session.record_start()
    await session.wait_while_recording(RECORD_SECONDS)
    await session.record_stop()

    await session.record_start()
    await session.wait_while_recording(RECORD_SECONDS)
    await session.record_stop()

    ok = False
    try:
        await session.confirm_new_clip(clips_before)
    except BMDVerificationError as exc:
        if "new clips" in str(exc):
            print(f"  PASS — raised as expected: {exc}")
            ok = True
        else:
            print(f"  FAIL — raised, but not the expected message: {exc}")
    else:
        print("  FAIL — did not raise")

    clips_after = await session.clips()
    ids_before = {clip.clip_unique_id for clip in clips_before}
    new_ids = sorted(
        clip.clip_unique_id for clip in clips_after if clip.clip_unique_id not in ids_before
    )
    print(f"\n=== Cleaning up {new_ids} ===")
    if new_ids:
        result = await session.delete_clips(new_ids, confirm=True)
        print(f"  deleted: {[c.clip_unique_id for c in result.deleted]}")
        if result.failed:
            print(f"  FAILED to delete: {result.failed}")

    return ok


async def run(args: argparse.Namespace) -> int:
    async with RestCameraSession(args.host, args.model_key, args.firmware) as session:
        results = {}

        try:
            results["zero_new_clips"] = await test_zero_new_clips(session)
        except (BMDVerificationError, BMDStorageError) as exc:
            print(f"  Test 1 ERROR: {exc}")
            results["zero_new_clips"] = False

        try:
            storage_ok = await test_storage_before_omitted_and_no_active_device(session)
            results["storage_before_omitted"] = storage_ok
            results["storage_before_no_active_device"] = storage_ok
        except (BMDVerificationError, BMDStorageError) as exc:
            print(f"  Tests 2/3 ERROR: {exc}")
            results["storage_before_omitted"] = False
            results["storage_before_no_active_device"] = False

        try:
            results["more_than_one_new_clip"] = await test_more_than_one_new_clip(session)
        except (BMDVerificationError, BMDStorageError) as exc:
            print(f"  Test 4 ERROR: {exc}")
            results["more_than_one_new_clip"] = False

        print("\n=== Summary ===")
        for name, passed in results.items():
            print(f"  {'PASS' if passed else 'FAIL'} — {name}")
        print(
            "  SKIPPED — storage_after (fresh) has no active device: not practically "
            "forceable without physically pulling the SD card, which would very likely "
            "fail clips() itself first instead of reaching this branch. See this "
            "script's own module docstring for the full reasoning."
        )

        all_passed = all(results.values())
        print(f"\n{'ALL TESTS PASSED' if all_passed else 'SOME TESTS FAILED'}")

    return 0 if all_passed else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify RestCameraSession.confirm_new_clip()'s defensive branches "
            "against real hardware."
        )
    )
    parser.add_argument("--host", required=True, help="Camera hostname or IP.")
    parser.add_argument("--model-key", required=True)
    parser.add_argument("--firmware", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(run(parse_args())))
