"""
Set the camera to a full (codec, quality variant, resolution, fps)
combination via RestCameraSession.set_camera_format — the REST analogue of
CameraSession.set_camera_format, but a single PUT /system/format instead of
three separate BLE packets.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: codec family, quality variant,
resolution, and frame rate — twice (ProRes, then back to BRAW). Note your
camera's current settings before running so you can restore them.

set_camera_format checks the requested combination against the camera's own
live GET /system/supportedFormats capability matrix before writing anything,
raising BMDUnsupportedError immediately if the camera doesn't report
offering it — no BLE-side dimension_enum/known_unreachable/max_fps_int
tables are consulted (those stay BLE-only). Every write is then verified via
the same WS-event-primary/GET-readback-secondary dual-check
record_start/record_stop already use, raising BMDVerificationError unless
confirmed.

WHY THIS COMBINATION MATTERS: ProRes/4K DCI is exactly the combination
docs/ble/settings.md records as `known_unreachable` over BLE — this
codebase's BLE write path cannot reach it despite nine falsification
attempts across three sessions, even though the camera itself demonstrably
supports it (confirmed through its own body menu, and independently via
GET /system/supportedFormats — see docs/rest/transport.md). If this script
confirms the write, it's the clearest proof yet that REST solves a real
software gap BLE could not — see set_camera_format's own docstring.

Usage:
    python examples/rest_change_format.py

Edit HOST / MODEL_KEY / FIRMWARE and COMBINATIONS below to target a
different camera or try different (codec, variant, resolution, fps)
targets. The default fps (23.98) is not guaranteed to be offered at every
combination on every camera — set_camera_format's own live capability
check is what actually decides that, not this script.
"""

import asyncio
import logging
import sys

from bmd_camera import BMDUnsupportedError, BMDVerificationError, RestCameraSession

HOST = "pocket-cinema-camera-6k-pro.local"
MODEL_KEY = "POCKET_6K_PRO"
FIRMWARE = "v8.6"
# HOST = "pocket-cinema-camera-6k-g2.local"
# MODEL_KEY = "POCKET_6K_G2"
# FIRMWARE = "v8.6"

FPS = "23.98"
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
    print(f"{label} confirmed ✓")
    return True


async def main() -> None:
    results: list[tuple[str, bool]] = []

    async with RestCameraSession(HOST, MODEL_KEY, FIRMWARE) as session:
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
    # Same rationale as examples/rest_record_start_stop.py: keep print() and
    # logging output chronologically interleaved in captured log files.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
