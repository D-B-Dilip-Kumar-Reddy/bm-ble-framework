"""
Set the camera to a full (codec, quality variant, resolution, fps)
combination via CameraSession.set_camera_format — the orchestration method
that sequences set_video_format / set_codec_quality / set_recording_format
(see docs/settings.md §9 and set_camera_format's own docstring) so a script
doesn't need to know which of those three settings packets accomplishes
which part, or that one combination (4K DCI/ProRes) needs a two-step
workaround because its dimension_enum is still unknown.

All three settings families are VERIFIED on real hardware (docs/settings.md
§8, §10), including each one's no-echo-on-redundant-write behavior (§11,
§14) — set_camera_format's steps silently skip a write that's already
known to be satisfied rather than raising. Every step still raises
BMDVerificationError unless confirmed by an INCOMING_CONTROL echo when a
write does happen — this script never assumes success from "the call
didn't raise".

WHAT THIS SCRIPT CHANGES ON THE CAMERA: codec family, quality variant,
resolution, and frame rate — twice (ProRes, then back to BRAW). Note your
camera's current settings before running so you can restore them.

Usage:
    python examples/change_codec.py

Edit COMBINATIONS below to try different (codec, variant, resolution, fps)
targets. The default pair demonstrates both code paths set_camera_format
takes: plain BRAW/4K DCI has a known dimension_enum and switches directly;
ProRes/4K DCI has none, so set_camera_format proxies through UHD first
(the pixel-dimension-closest ProRes resolution that does have one) before
set_recording_format lands the actual 4K DCI target.
"""

import asyncio
import logging
import sys

from bmd_ble import BMDUnsupportedError, BMDVerificationError, CameraSession

MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v7.9"

FPS = "25"
COMBINATIONS = [
    ("ProRes", "422", "4K DCI", FPS),
    ("BRAW", "5:1", "4K DCI", FPS),
]

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
        for codec, variant, resolution, fps in COMBINATIONS:
            label = f"set {codec} {variant} {resolution} @ {fps}"
            print(f"\n=== {label} ===")
            ok = await _step(
                label,
                lambda c=codec, v=variant, r=resolution, f=fps: session.set_camera_format(
                    c, v, r, f
                ),
            )
            results.append((label, ok))
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
