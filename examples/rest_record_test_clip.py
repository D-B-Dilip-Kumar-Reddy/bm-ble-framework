"""
Record a real clip of a fixed length at a specific (codec, quality variant,
resolution, fps) combination, entirely over REST, with camera/media state
captured before and after — the closest thing this repo has to a full
end-to-end "shoot a test clip and report on it" script.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: switches the camera to
CODEC/VARIANT/RESOLUTION/FPS (see set_camera_format's own docstring for
what that means for other in-progress settings), then records a real clip
of RECORD_SECONDS length, consuming real storage space and real time.
Note your camera's current settings before running if you need to restore
them afterward.

VERIFICATION: every write here (set_camera_format, record_start,
record_stop) uses the same dual-check RestCameraSession always does — a WS
propertyValueChanged event primary, a GET readback secondary,
BMDVerificationError if neither confirms. The recording hold uses
wait_while_recording(RECORD_SECONDS), which returns early if the camera
stops on its own (e.g. a full card) rather than blindly sleeping — see
examples/rest_record_start_stop.py, which this script's recording step is
modeled on.

STATE IS PRINTED AT THREE POINTS, not two: "BEFORE any changes" (the
camera exactly as this script found it, before set_camera_format() touches
anything), "BEFORE recording" (after the format switch, immediately before
record_start()), and "AFTER recording" (after record_stop() confirms).
Each prints format (camera settings), storage_state() (media — device
name, total/remaining space, remaining record time, clip count), and
clips() (clip inventory).

The first two are NOT redundant, and the gap between them is itself worth
reading: on a real run (POCKET_6K_G2 v8.6, 2026-08-05) the active device's
remaining_record_time was still reporting the PRE-switch format's estimate
immediately after set_camera_format() returned confirmed — 50858s (14h07m)
for a card that, once recording actually started, reported 15251s (4h14m)
for the same 996GB free. The camera had not recomputed the estimate for
the new format yet. remaining_record_time immediately after a format
change is therefore a stale number, not a current one; storage_state()'s
remaining_space stayed accurate throughout.

The clip inventory taken before recording is kept so the newly-written
clip can be identified afterward by clip_unique_id — GET /clips/list has
no "just-written" flag of its own (design principle 9: reads are
best-effort, not proof of anything not directly reported), so a
before/after diff is the only way to name which clip is new.

AFTER RECORDING: the new clip's file_path (name) and duration_timecode
(length) come straight from its own Clip entry; "memory used" is computed
as the active storage device's remaining_space before minus after — not a
number the camera reports about the clip directly, since Clip carries no
size field (Phase 6's rest/media.py hit the same gap for stills).

Usage:
    python examples/rest_record_test_clip.py

Edit HOST / MODEL_KEY / FIRMWARE and CODEC / VARIANT / RESOLUTION / FPS /
RECORD_SECONDS below to target a different camera or combination. Shorten
RECORD_SECONDS for a quick smoke test before committing to a full 10
minutes.
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

CODEC = "BRAW"
VARIANT = "5:1"
RESOLUTION = "4K DCI"
FPS = "23.98"

RECORD_SECONDS = 600  # 10 minutes


def _format_bytes(num_bytes: int) -> str:
    gb = num_bytes / 1_000_000_000
    return f"{num_bytes} bytes ({gb:.2f} GB)"


def _format_seconds(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{seconds}s ({hours:d}h{minutes:02d}m{secs:02d}s)"


async def _print_state(session: RestCameraSession, label: str):
    """Print camera settings, media state, and clip inventory. Returns
    (clips, active_device) so callers can diff/compare without re-fetching
    the same state a second time."""
    print(f"\n--- {label}: camera settings ---")
    fmt = await session.get_format()
    width, height = fmt.record_resolution
    print(f"  codec={fmt.codec} resolution={width}x{height} frameRate={fmt.frame_rate}")

    print(f"--- {label}: media state ---")
    storage = await session.storage_state()
    device = storage.active_device
    if device is None:
        print("  No active storage device reporting.")
    else:
        print(f"  device={device.device_name!r} volume={device.volume}")
        print(f"  total space:     {_format_bytes(device.total_space)}")
        print(f"  remaining space: {_format_bytes(device.remaining_space)}")
        print(f"  remaining time:  {_format_seconds(device.remaining_record_time)}")
        print(f"  clip count:      {device.clip_count}")

    print(f"--- {label}: clip inventory ---")
    try:
        clips = await session.clips()
        print(f"  {len(clips)} clip(s) on card")
    except BMDStorageError as exc:
        print(f"  {exc}")
        clips = ()

    return clips, device


async def main() -> int:
    async with RestCameraSession(HOST, MODEL_KEY, FIRMWARE) as session:
        await _print_state(session, "BEFORE any changes")

        try:
            await session.set_camera_format(CODEC, VARIANT, RESOLUTION, FPS)
        except (ValueError, BMDUnsupportedError, BMDVerificationError) as exc:
            print(f"set_camera_format({CODEC}, {VARIANT}, {RESOLUTION}, {FPS}) failed: {exc}")
            return 1
        print(f"Camera format set: {CODEC} {VARIANT} {RESOLUTION} {FPS}")

        clips_before, device_before = await _print_state(session, "BEFORE recording")
        ids_before = {clip.clip_unique_id for clip in clips_before}

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

        clips_after, device_after = await _print_state(session, "AFTER recording")

        new_clips = [clip for clip in clips_after if clip.clip_unique_id not in ids_before]

        print("\n=== Captured clip ===")
        if not new_clips:
            print(
                "No new clip found — GET /clips/list did not report one that wasn't "
                "already there before recording started."
            )
            return 1
        for clip in new_clips:
            print(f"  clip_unique_id: {clip.clip_unique_id}")
            print(f"  name:           {clip.file_path}")
            print(f"  length:         {clip.duration_timecode}")
            print(f"  codec:          {clip.codec}")
            print(f"  video format:   {clip.video_format}")

        if device_before is not None and device_after is not None:
            used = device_before.remaining_space - device_after.remaining_space
            print(f"  memory used:    {_format_bytes(used)}")
        else:
            print("  memory used:    unknown — no active storage device before/after")

    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
