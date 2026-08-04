"""
Exercise RestCameraSession's Phase 7 playback surface — build a timeline
from the camera's own clips, enter playback, play/pause/seek/shuttle, then
leave playback mode again. Entirely new capability BLE never reached
(docs/rest/transport.md's "New capability REST brings").

WHAT THIS SCRIPT CHANGES ON THE CAMERA: switches to playback mode and
scrubs through footage. Does not record, format, or delete anything, but
does leave the camera in playback mode if a step raises partway through —
press stop/exit on the camera body if that happens.

VERIFICATION IS BY EYE, NOT BY THIS SCRIPT — read this before running
--------------------------------------------------------------------------------
Every write here is verified the same dual-check way as every other
RestCameraSession write (a WS propertyValueChanged event primary, a GET
readback secondary, BMDVerificationError if neither confirms). Two field
shapes are now real-hardware-confirmed (POCKET_6K_PRO v8.6, 2026-08-04):
/transports/0/playback's body ({"type", "loop", "singleClip", "speed",
"position"}, see RestCameraSession._put_playback's docstring) and
/timelines/0/add's POST body ({"clips": [{"clipUniqueId": ...}, ...]}, see
set_timeline's docstring — confirmed via direct Postman testing to be the
only accepted shape of five tried). That confirmed body is not the same as
a working set_timeline(): a follow-up Postman session found it can return
204 while leaving an existing timeline entry completely unchanged — see
FORMAT PRECONDITION below for the leading hypothesis why. Every step from
enter_playback() onward remains unexercised by a real run of this script.
Watch the camera's own screen for the real ground truth, the same way
docs/rest/transport.md's own Phase 7 note says to
(`python examples/rest_playback.py` — operator watches the camera screen).

FORMAT PRECONDITION — observed physically on the camera body
(POCKET_6K_PRO v8.6, 2026-08-04): a clip only plays if the camera's
*current* codec/quality/resolution/fps matches the format the clip was
recorded with. If this script's chosen clip doesn't match, switch the
camera's format first (RestCameraSession.set_camera_format, Phase 5) —
this script does not do that for you. See enter_playback()'s docstring.
A follow-up Postman session hit a live instance of this same constraint
one step earlier than expected: adding a ProRes clip to a timeline that
already held a BRAW clip silently did nothing (204, no change) — see
set_timeline()'s docstring for the unconfirmed hypothesis that this
precondition applies at timeline-build time too, not just at playback
start. If this script's set_timeline() step fails, try picking a clip
whose format matches whatever the camera is currently set to before
assuming the request shape is wrong.

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

CLIP_COUNT_FOR_TIMELINE = 1
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

        timeline_ids = [clip.clip_unique_id for clip in clips[:CLIP_COUNT_FOR_TIMELINE]]
        print(f"Building timeline from clip id(s) {timeline_ids} ({clips[0].file_path})")

        steps: list[tuple[str, object]] = [
            ("set_timeline", lambda: session.set_timeline(timeline_ids)),
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
