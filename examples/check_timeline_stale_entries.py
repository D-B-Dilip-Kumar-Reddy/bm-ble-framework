"""
Check whether select_clip()'s DELETE-then-POST timeline sync leaves stale
cross-format entries behind when DELETE isn't implemented.

Real-hardware finding (docs/rest/session.md's select_clip() section,
finding #1): DELETE /timelines/0 returns 501 on this firmware, so
select_clip() always falls through to POST /timelines/0/add without an
explicit clear first. Whether that ever leaves a *previous* format's
clips mixed in with the *new* format's clips after a switch has never
been tested through select_clip()'s own code path — every select_clip()
run so far only ever requested one format at a time.

This script takes two clip_unique_ids whose recorded formats differ,
switches between them via select_clip(), and reads RestCameraSession.
timeline_clip_ids() (a plain GET /timelines/0, independent of
select_clip()'s own internal "is my clip in there" poll) after each
switch. It then checks every id the second readback reports against
clips() — any id whose own format doesn't match the second clip's format
is a stale, cross-format leftover.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: switches the camera's recording
format twice (once per requested clip — select_clip()'s own behaviour,
see its docstring), and leaves the camera at the second clip's format
when the script ends. Does not call enter_playback()/play()/exit_playback()
at all, deliberately — exit_playback()'s confirmed format-revert
(docs/rest/session.md's enter_playback() / exit_playback() section) would
otherwise muddy what this script is trying to isolate.

Usage:
    python examples/check_timeline_stale_entries.py <clip_id_a> <clip_id_b>

<clip_id_a> and <clip_id_b> are Clip.clip_unique_id values — run
examples/rest_read_state.py first, or check a prior rest_playback.py run's
"Selecting clip_unique_id=..." line, to find them. The two clips should
have different codec/resolution/fps (Clip.codec / Clip.video_format from
clips()) — the script warns if they don't, since the test is meaningless
otherwise.
"""

import asyncio
import logging
import sys

from bmd_camera import BMDUnsupportedError, BMDVerificationError, RestCameraSession
from bmd_camera.exceptions import BMDStorageError

HOST = "pocket-cinema-camera-6k-g2.local"
MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v8.6"
# HOST = "pocket-cinema-camera-6k-pro.local"
# MODEL_KEY = "POCKET_6K_PRO"
# FIRMWARE = "v8.6"


async def _select_and_read(session: RestCameraSession, label: str, clip_id: int) -> tuple[int, ...]:
    print(f"\n=== select_clip({clip_id}) — {label} ===")
    await session.select_clip(clip_id)
    ids = await session.timeline_clip_ids()
    print(f"GET /timelines/0 after selecting {label}: {ids}")
    return ids


async def main(clip_id_a: int, clip_id_b: int) -> int:
    async with RestCameraSession(HOST, MODEL_KEY, FIRMWARE) as session:
        try:
            clips = await session.clips()
        except BMDStorageError as exc:
            print(f"clips(): {exc}")
            return 1

        by_id = {clip.clip_unique_id: clip for clip in clips}
        for clip_id in (clip_id_a, clip_id_b):
            if clip_id not in by_id:
                print(f"clip_unique_id={clip_id} not found in GET /clips/list")
                return 1

        clip_a, clip_b = by_id[clip_id_a], by_id[clip_id_b]
        print(
            f"Clip A: clip_unique_id={clip_a.clip_unique_id} {clip_a.codec} "
            f"{clip_a.video_format} ({clip_a.file_path})"
        )
        print(
            f"Clip B: clip_unique_id={clip_b.clip_unique_id} {clip_b.codec} "
            f"{clip_b.video_format} ({clip_b.file_path})"
        )
        if (clip_a.codec, clip_a.video_format) == (clip_b.codec, clip_b.video_format):
            print(
                "\nWarning: clips A and B report the SAME codec/videoFormat — this test "
                "needs two different formats to be meaningful."
            )

        try:
            await _select_and_read(session, "A", clip_id_a)
            ids_after_b = await _select_and_read(session, "B", clip_id_b)
        except (BMDVerificationError, BMDUnsupportedError) as exc:
            print(f"\nFAILED: {exc}")
            return 1

        foreign = [
            clip_id
            for clip_id in ids_after_b
            if clip_id in by_id
            and (by_id[clip_id].codec, by_id[clip_id].video_format)
            != (clip_b.codec, clip_b.video_format)
        ]

    print("\n=== Result ===")
    if foreign:
        print(
            f"STALE ENTRIES FOUND: {foreign} — these clips do not match clip B's format "
            f"({clip_b.codec} {clip_b.video_format}) but are still in the timeline after "
            "selecting B."
        )
        return 1
    print(
        "No stale entries — every id in the timeline after selecting B matches clip B's own format."
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: python {sys.argv[0]} <clip_id_a> <clip_id_b>")
        raise SystemExit(2)
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main(int(sys.argv[1]), int(sys.argv[2]))))
