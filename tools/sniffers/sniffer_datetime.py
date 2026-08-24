"""
tools/sniffers/sniffer_datetime.py
===================================
Passive sniffer for BLE SDI Category 7 ("Configuration") — Real Time Clock
(7.0), System language (7.1), Timezone (7.2) — see docs/ble/datetime.md.

**Status: this category has never been sniffed on any camera in this repo.**
Everything docs/ble/protocol.md says about it (`int32 x2` payload — [0] time
BCD, [1] date BCD YYYYMMDD) is [spec] only, transcribed from the official
document, not [sniffer-verified] (design principle 6 — every protocol value
must come from a real capture on the specific camera/firmware, never from the
spec alone). This tool is how that capture gets taken.

Two modes:

1. Default (`--actions`): runs one interactive capture window per action
   label, exactly like sniffer_settings.py. The operator triggers each change
   on the physical camera's SETUP menu between two Enter presses; the tool
   only listens.
2. `--burst-seconds N`: connect-burst mode — no operator action at all.
   Listens for N seconds starting immediately after subscribing, to test
   whether the camera announces state (Category 7 included) unprompted right
   after a BLE client connects. Added after two real-hardware `--actions`
   runs (docs/ble/datetime.md §4, §5) found zero Category 7 traffic even
   across genuine committed date/time/timezone changes — this mode exists to
   rule in or out a capture-timing gap as the explanation, before escalating
   to an active write probe.

Either way, every window prints every (characteristic, category, parameter)
triple observed on INCOMING_CONTROL / CAMERA_STATUS and saves the full
decoded capture to tools/captures/.

WHY THIS MATTERS BEYOND THE CATEGORY ITSELF
---------------------------------------------
`rest/media.py`'s `guess_new_still_path()` has a confirmed, permanent failure
mode: it guesses a still's filename from the *operator's PC clock*, and a
real run (POCKET_6K_G2 v8.6, 2026-08-13) found the camera's own onboard clock
running ~37h21m behind, causing every candidate to miss. Its own docstring
names the fix: "a caller who knows the camera's clock is wrong should pass a
minute_offsets/around combination derived from the camera's own reported
time." Category 7.0 is that "camera's own reported time" — once BLE can read
it, `around` can be built from the camera's real clock instead of the PC's,
closing this gap for good rather than requiring the operator to notice skew
and fix it by hand.

WHAT WE DON'T YET KNOW
-------------------------
The public spec only documents ASSIGN (write) and this repo has never
observed a general "read current value" operation (docs/ble/protocol.md §4
— only ASSIGN, OFFSET, and CAMERA_REPORT have ever been seen). Every other
settings family in this codebase is read the same way: the camera spontaneously
CAMERA_REPORTs a parameter when it *changes*, not on request — so the plan
here is the same as Category 0x0A/0x01's: capture what the camera reports
when the operator changes date/time/timezone on the body, not "ask" it.
Whether the camera also reports 7.0 unprompted right after connecting/
subscribing (a "state burst") is a secondary, weaker possibility this tool's
default windows are not positioned to catch reliably (the burst, if any,
would land in the gap between subscribing and the first window opening) —
change_* windows are the primary target.

DEFAULT ACTIONS
------------------
- view_setup_datetime: operator opens SETUP > Date/Time without changing
  anything. Cheap to try; only useful if merely opening the screen queries
  or re-reports the value, which is not expected but costs nothing to check.
- change_date: operator changes the date by one day in the menu (then can
  change it back afterward — this tool doesn't care about the camera's final
  state).
- change_time: operator changes the time by a few minutes.
- change_timezone: operator changes the timezone, if it's a separate control
  from date/time on this camera's menu (7.2 is a distinct parameter in the
  spec table).

Usage:
    python tools/sniffers/sniffer_datetime.py
    python tools/sniffers/sniffer_datetime.py --model-key POCKET_6K_PRO --firmware v8.6
    python tools/sniffers/sniffer_datetime.py --actions change_date,change_time
    python tools/sniffers/sniffer_datetime.py --burst-seconds 10
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from capture import (  # noqa: E402
    configure_console_logging,
    print_window_summary,
    run_capture_windows,
    run_immediate_burst_capture,
    save_capture,
)

from bmd_camera.ble.camera_controller import BMDCameraController  # noqa: E402
from bmd_camera.ble.scanner import scan_for_camera  # noqa: E402
from bmd_camera.camera_profile import CameraProfile  # noqa: E402

DEFAULT_MODEL_KEY = "POCKET_6K_G2"
DEFAULT_FIRMWARE = "v8.6"
DEFAULT_ACTION_LABELS = [
    "view_setup_datetime",
    "change_date",
    "change_time",
    "change_timezone",
]


async def run(args: argparse.Namespace) -> int:
    profile = CameraProfile.for_model(model_key=args.model_key, firmware=args.firmware)
    discovered = await scan_for_camera(profile.ble_name, timeout=args.timeout)
    cam = BMDCameraController(discovered=discovered, profile=profile)

    await cam.connect()
    try:
        if args.burst_seconds is not None:
            print(
                f"\nConnect-burst mode: listening for {args.burst_seconds}s starting "
                "immediately after subscribing, no operator action needed — tests "
                "whether the camera announces state (Category 7 included) unprompted "
                "right after a BLE client connects, which the operator-triggered "
                "--actions windows structurally cannot catch (docs/ble/datetime.md §5)."
            )
            session = await run_immediate_burst_capture(cam, listen_seconds=args.burst_seconds)
        else:
            labels = [label.strip() for label in args.actions.split(",") if label.strip()]
            if not labels:
                raise SystemExit("--actions must name at least one capture window label.")
            session = await run_capture_windows(cam, labels)
            for window in session.windows:
                print_window_summary(window)

        saved_path = save_capture(args.model_key, args.firmware, session)
        print(f"\nCapture saved to: {saved_path}")
        print(
            "\nLook for a (characteristic, category=0x07, parameter) triple in the "
            "windows above — parameter 0x00 is Real Time Clock, 0x01 is System "
            "language, 0x02 is Timezone (docs/ble/protocol.md Category 7 table). "
            "Share the saved capture file's relevant window(s) to decode the "
            "payload against the operator-confirmed before/after values."
        )
    finally:
        await cam.disconnect()

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sniff Category 7 (Real Time Clock / language / timezone) BLE "
            "notifications from a camera while the operator changes each "
            "setting on the body's SETUP menu."
        )
    )
    parser.add_argument(
        "--model-key",
        default=DEFAULT_MODEL_KEY,
        help=f"Camera model key used to load CameraProfile. Default: {DEFAULT_MODEL_KEY}",
    )
    parser.add_argument(
        "--firmware",
        default=DEFAULT_FIRMWARE,
        help=f"Camera firmware used to load CameraProfile. Default: {DEFAULT_FIRMWARE}",
    )
    parser.add_argument(
        "--actions",
        default=",".join(DEFAULT_ACTION_LABELS),
        help=(
            "Comma-separated capture window labels, one window per operator-"
            "triggered change. Ignored when --burst-seconds is given. "
            "Default: " + ",".join(DEFAULT_ACTION_LABELS)
        ),
    )
    parser.add_argument(
        "--burst-seconds",
        type=float,
        default=None,
        help=(
            "Connect-burst mode instead of --actions: listen for this many "
            "seconds starting immediately after subscribing, no operator "
            "action needed. Tests whether the camera announces state "
            "unprompted right after connecting (docs/ble/datetime.md §5) — "
            "run this with no other setup needed. Not combined with --actions."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="BLE scan timeout in seconds. Default: 15.0",
    )

    return parser.parse_args()


if __name__ == "__main__":
    configure_console_logging()
    raise SystemExit(asyncio.run(run(parse_args())))
