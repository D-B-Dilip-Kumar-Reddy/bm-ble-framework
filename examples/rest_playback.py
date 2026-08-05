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

VERIFICATION IS BY EYE, NOT ONLY BY THIS SCRIPT — read this before running
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
alone there.

This exact combination — the format check, the switch when needed, and the
timeline sync — is now real-hardware-confirmed: a full run of this script
(select_clip through exit_playback, all nine steps) passed clean on both
POCKET_6K_G2 and POCKET_6K_PRO v8.6, 2026-08-04, every step's own
dual-check confirming. That run's clip already matched the camera's
current format on both cameras, so it didn't exercise select_clip()'s
set_camera_format() branch specifically — a clip whose format doesn't
match remains the next case to confirm. This script's own dual-check
passing is real evidence the writes and their confirmation channels work;
it is not the same as an operator watching the footage actually play, so
still watch the camera's own screen for that ground truth, the same way
docs/rest/transport.md's own Phase 7 note says to.

CAVEAT ADDED 2026-08-05, READ BEFORE TRUSTING "the POST selected the clip":
tools/rest/diagnose_timeline.py found that POST /timelines/0/add is a
no-op when the target clip's format doesn't already match the camera's
live format — no error, no timeline change, ever. Every confirmed success
above (this script's own run included) switched format to match the
target clip before POSTing, so none of them can actually distinguish "the
POST did the selecting" from "the format switch alone already would have
populated the timeline, POST or not." See docs/rest/session.md's
select_clip() section, finding #7 — this script's own passing runs are
still real evidence the end-to-end sequence works, just not proof of what
specifically inside it is doing the work.

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
# stop() is currently an alias for exit_playback() (see session.py's docstring for why) —
# both send the identical PUT /transports/0 {"mode": "InputPreview"}. Whatever's visible on
# the camera screen happens at the "stop" step; this longer pause just gives an operator
# time to actually look before the (functionally redundant) "exit_playback" step re-fires
# the same call.
STOP_TO_EXIT_PAUSE_S = 8.0


async def _print_format(session: RestCameraSession, label: str) -> None:
    fmt = await session.get_format()
    width, height = fmt.record_resolution
    print(f"{label}: {fmt.codec} @ {width}x{height}p{fmt.frame_rate}")


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
        await _print_format(session, "Format before select_clip")

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
                pause_s = STOP_TO_EXIT_PAUSE_S if label == "stop" else PAUSE_BETWEEN_STEPS_S
                await asyncio.sleep(pause_s)

        await _print_format(session, "Format after exit_playback")

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
