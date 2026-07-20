"""
Switch the camera between BRAW and ProRes and back, exercising the two
settings write paths CameraSession exposes (see docs/settings.md):

  1. set_video_format — the FORMAT packet whose dimension_enum locks
     resolution AND codec family together. This is the packet that actually
     switches BRAW <-> ProRes (the codec_quality packet alone does not).
  2. set_codec_quality — sets the quality variant within the now-active
     codec family (e.g. BRAW 5:1, ProRes HQ).

Every write raises BMDVerificationError unless confirmed by an
INCOMING_CONTROL echo — this script never assumes success from "the write
didn't raise". The settings families are CANDIDATE (transcribed from an
external reverse-engineering document, echo behaviour not yet captured), so
a "no echo received" failure here is itself a useful finding: capture the
real response with tools/control/send_settings_command.py and update the
profile provenance — see docs/settings.md's verification runbook.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: resolution, codec family, quality
variant, and frame rate. Note your camera's current settings before running
so you can restore them.

Usage:
    python examples/change_codec.py

Edit the constants below to target a different camera or combination. The
defaults use 4K DCI — the only POCKET_6K_G2 v7.9 resolution offered under
both codec families.
"""

import asyncio
import logging
import sys

from bmd_ble import BMDUnsupportedError, BMDVerificationError, CameraSession

MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v7.9"

FPS = "25"
# 4K DCI is the only G2 resolution shared by BRAW and ProRes, so the
# BRAW <-> ProRes round trip below keeps the resolution constant. NOTE: the
# 4K DCI ProRes dimension_enum has not been captured yet, so the ProRes leg
# uses UHD instead — swap back to "4K DCI" once the enum is in the profile.
BRAW_RESOLUTION = "4K DCI"
BRAW_VARIANT = "5:1"
PRORES_RESOLUTION = "UHD"
PRORES_VARIANT = "HQ"

PAUSE_BETWEEN_STEPS_S = 3.0


async def _step(label: str, action) -> bool:
    try:
        await action()
    except BMDVerificationError as exc:
        print(f"{label} NOT confirmed: {exc}")
        return False
    except (BMDUnsupportedError, ValueError) as exc:
        print(f"{label} not attempted: {exc}")
        return False
    print(f"{label} confirmed by echo ✓")
    return True


async def main() -> None:
    results: list[tuple[str, bool]] = []

    async with CameraSession(MODEL_KEY, FIRMWARE) as session:
        steps = [
            (
                f"switch to ProRes ({PRORES_RESOLUTION} @ {FPS})",
                lambda: session.set_video_format(PRORES_RESOLUTION, "ProRes", FPS),
            ),
            (
                f"set ProRes variant {PRORES_VARIANT}",
                lambda: session.set_codec_quality("ProRes", PRORES_VARIANT),
            ),
            (
                f"switch back to BRAW ({BRAW_RESOLUTION} @ {FPS})",
                lambda: session.set_video_format(BRAW_RESOLUTION, "BRAW", FPS),
            ),
            (
                f"set BRAW variant {BRAW_VARIANT}",
                lambda: session.set_codec_quality("BRAW", BRAW_VARIANT),
            ),
        ]

        for label, action in steps:
            print(f"\n=== {label} ===")
            results.append((label, await _step(label, action)))
            await asyncio.sleep(PAUSE_BETWEEN_STEPS_S)

    print("\n=== Summary ===")
    for label, ok in results:
        print(f"  {'OK    ' if ok else 'FAILED'}  {label}")

    if not all(ok for _, ok in results):
        raise SystemExit(1)


if __name__ == "__main__":
    # Same rationale as examples/record_start_stop.py: keep print() and
    # logging output chronologically interleaved in captured log files.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
