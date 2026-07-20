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
didn't raise". `set_video_format` is VERIFIED (2026-07-20, docs/settings.md
§8 — this very script's echo verification is part of that evidence);
`set_codec_quality` is still CANDIDATE.

REAL-HARDWARE GOTCHA (2026-07-20, docs/settings.md §8, also documented on
`CameraSession.set_codec_quality`'s own docstring): a `video_format` switch
resets the active codec family's quality to a per-family *remembered*
value (whatever that family's `codec_quality` was last set to). If this
script's `set_codec_quality` call then asks for that exact same value, the
camera treats it as a no-op and never reports — `set_codec_quality` then
raises `BMDVerificationError("no echo received")`, which looks like a
protocol failure but isn't. To make each codec_quality leg a *genuine*
test regardless of what the camera remembers, each leg sends two different
variants in sequence — the first may harmlessly no-op (not counted toward
the summary), the second is guaranteed to be a real change from whatever
came before, so its confirmation actually exercises the write+echo path.

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
# 4K DCI ProRes dimension_enum is still unknown (searched exhaustively over
# 0x01-0x16, docs/settings.md §7-§8) so the ProRes leg uses UHD instead —
# swap back to "4K DCI" once the enum is found.
BRAW_RESOLUTION = "4K DCI"
PRORES_RESOLUTION = "UHD"
# Two distinct variants per family — see the REAL-HARDWARE GOTCHA note
# above for why a single fixed target isn't a reliable write+echo test.
PRORES_VARIANTS = ("HQ", "422")
BRAW_VARIANTS = ("5:1", "Q5")

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
    results: list[tuple[str, bool]] = []  # counted toward the exit status
    primed: list[tuple[str, bool]] = []  # informational only — may harmlessly no-op

    async with CameraSession(MODEL_KEY, FIRMWARE) as session:
        first, second = PRORES_VARIANTS
        steps = [
            (
                f"switch to ProRes ({PRORES_RESOLUTION} @ {FPS})",
                lambda: session.set_video_format(PRORES_RESOLUTION, "ProRes", FPS),
                results,
            ),
            (
                f"prime ProRes variant {first} (may harmlessly no-op)",
                lambda: session.set_codec_quality("ProRes", first),
                primed,
            ),
            (
                f"set ProRes variant {second}",
                lambda: session.set_codec_quality("ProRes", second),
                results,
            ),
            (
                f"switch back to BRAW ({BRAW_RESOLUTION} @ {FPS})",
                lambda: session.set_video_format(BRAW_RESOLUTION, "BRAW", FPS),
                results,
            ),
        ]
        first, second = BRAW_VARIANTS
        steps += [
            (
                f"prime BRAW variant {first} (may harmlessly no-op)",
                lambda: session.set_codec_quality("BRAW", first),
                primed,
            ),
            (
                f"set BRAW variant {second}",
                lambda: session.set_codec_quality("BRAW", second),
                results,
            ),
        ]

        for label, action, bucket in steps:
            print(f"\n=== {label} ===")
            bucket.append((label, await _step(label, action)))
            await asyncio.sleep(PAUSE_BETWEEN_STEPS_S)

    print("\n=== Summary ===")
    for label, ok in results:
        print(f"  {'OK    ' if ok else 'FAILED'}  {label}")
    if primed:
        print("\n(priming steps — a FAILED here is an expected no-op, not a bug)")
        for label, ok in primed:
            print(f"  {'OK    ' if ok else 'no-op '}  {label}")

    if not all(ok for _, ok in results):
        raise SystemExit(1)


if __name__ == "__main__":
    # Same rationale as examples/record_start_stop.py: keep print() and
    # logging output chronologically interleaved in captured log files.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
