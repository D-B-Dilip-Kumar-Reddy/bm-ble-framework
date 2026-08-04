"""
Exercise RestCameraSession's Phase 7 playback surface — select a clip from
the camera's own media, enter playback, play/pause/seek/shuttle, then leave
playback mode again. Entirely new capability BLE never reached
(docs/rest/transport.md's "New capability REST brings").

WHAT THIS SCRIPT CHANGES ON THE CAMERA: may switch the camera's recording
format (select_clip() does this automatically when needed — see below),
switches to playback mode, and scrubs through footage. Does not record or
delete anything, but does leave the camera in playback mode (and possibly a
different format than it started in) if a step raises partway through —
press stop/exit on the camera body if that happens, and check the format
before your next recording.

VERIFICATION IS BY EYE, NOT BY THIS SCRIPT — read this before running
--------------------------------------------------------------------------------
Every write here is verified the same dual-check way as every other
RestCameraSession write (a WS propertyValueChanged event primary, a GET
readback secondary, BMDVerificationError if neither confirms).
/transports/0/playback's body ({"type", "loop", "singleClip", "speed",
"position"}) and /timelines/0/add's POST body ({"clips":
[{"clipUniqueId": ...}]}) are both real-hardware-confirmed (POCKET_6K_PRO
v8.6, 2026-08-04) — see RestCameraSession._put_playback's and
select_clip()'s own docstrings. select_clip() itself replaces an earlier
set_timeline(clip_unique_ids: list[int]) design that direct Postman
debugging disproved outright: the camera has no concept of a caller-curated
playlist. Requesting one clip while the camera's format matched that clip
produced a GET /timelines/0 readback of every clip sharing that format —
seven clips, not one — confirmed two independent ways (the REST readback
itself, and the camera's own on-screen playback view showing "CLIP 1/7" for
the same group). select_clip() is built around that reality: it switches
format to match the requested clip, then confirms the clip appears
somewhere in the resulting (whole-format-group) timeline — not that it's
alone there. This exact combination has not itself been run against real
hardware yet; every step from select_clip() onward remains unexercised by
a real run of this script. Watch the camera's own screen for the real
ground truth, the same way docs/rest/transport.md's own Phase 7 note says
to (`python examples/rest_playback.py` — operator watches the camera
screen).

Usage:
    python examples/rest_playback.py
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

PLAY_DURATION_S = 3.0
PAUSE_BETWEEN_STEPS_S = 2.0


async def _step(label: str, action) -> bool:
    try:
        await action()
    except BMDVerificationError as exc:
        print(f"{label} NOT confirmed: {exc}")
        return False
    except (BMDUnsupportedError, ValueError) as exc:
        print(f"{label} not attempted: {exc}")
        return False
    print(f"{label} confirmed ✓ — check the camera screen")
    return True


async def main() -> int:
    results: list[tuple[str, bool]] = []

    async with RestCameraSession(HOST, MODEL_KEY, FIRMWARE) as session:
        try:
            clips = await session.clips()
        except BMDStorageError as exc:
            print(f"clips(): {exc}")
            return 1
        if not clips:
            print("No clips on the card — record at least one short clip first.")
            return 1

        target_clip = clips[0]
        print(f"Selecting clip_unique_id={target_clip.clip_unique_id} ({target_clip.file_path})")

        steps: list[tuple[str, object]] = [
            ("select_clip", lambda: session.select_clip(target_clip.clip_unique_id)),
            ("enter_playback", session.enter_playback),
            ("play", session.play),
        ]
        for label, action in steps:
            print(f"\n=== {label} ===")
            ok = await _step(label, action)
            results.append((label, ok))
            if not ok:
                break
            await asyncio.sleep(PLAY_DURATION_S if label == "play" else PAUSE_BETWEEN_STEPS_S)

        if all(ok for _, ok in results):
            for label, action in [
                ("pause", session.pause),
                ("seek(0)", lambda: session.seek(0)),
                ("shuttle(2.0) forward", lambda: session.shuttle(2.0)),
                ("shuttle(-1.0) backward", lambda: session.shuttle(-1.0)),
                ("stop", session.stop),
                ("exit_playback", session.exit_playback),
            ]:
                print(f"\n=== {label} ===")
                ok = await _step(label, action)
                results.append((label, ok))
                if not ok:
                    break
                await asyncio.sleep(PAUSE_BETWEEN_STEPS_S)

    print("\n=== Summary ===")
    for label, ok in results:
        print(f"  {'OK    ' if ok else 'FAILED'}  {label}")

    if not all(ok for _, ok in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
