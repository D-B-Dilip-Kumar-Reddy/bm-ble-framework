"""
tools/sniffers/sniffer_photo.py
================================
Passive sniffer for photo capture (still capture) — see docs/photo_capture.md.

Connects to the camera, then runs one interactive capture window per action
label. The operator triggers each photo on the physical camera (the body's
still/photo button) between two Enter presses; the tool only listens. For
each window it prints every (characteristic, category, parameter) triple
observed on INCOMING_CONTROL / CAMERA_STATUS and saves the full decoded
capture to tools/captures/.

The [spec] starting point is category 10 (Media), parameter 3 — "Still
Capture", a void trigger (docs/protocol.md §5). That is a map for reading
the capture, not a value to trust: whether this camera reports anything on
10.3 (or anywhere else) when a photo is taken from the body is exactly what
this sniffer determines.

Default windows and why:

  idle_baseline     Operator does NOTHING — captures the ambient telemetry
                    floor (e.g. categories 0x09/0x0C ticking ~1/s on the
                    G2). Photo capture is a single momentary action with no
                    natural paired action (unlike record_start/record_stop),
                    so this window supplies the contrast
                    tools/common/discovery.py's seed_triples_from_capture
                    ambient filter needs — without it, the filter has
                    nothing to compare against and keeps everything.
  photo_capture_1   Operator takes one photo. Three separate single-photo
  photo_capture_2   windows show whether the same triple fires on *every*
  photo_capture_3   capture (a genuine per-capture signal) rather than only
                    once (a one-time dump, like the connect-burst reports
                    seen during settings work — docs/settings.md).

Operational note (learned on the first real runs, 2026-07-27, both
cameras — see docs/photo_capture.md): after connect the camera drains a
large state-report burst over the indication channel at a throttled ~180ms
cadence, lasting 10s or more. Open the idle_baseline window only AFTER that
burst has finished — wait until notifications slow to the ~1/s ambient
cadence — or the burst contaminates the baseline and can even spill into
the first photo window (on the G2 its ordered 0x0C lens-string tail landed
inside photo_capture_1, where it could be misread as a photo-caused
report; the ~180ms spacing and ascending parameter order are the burst's
recognition signature).

Since a photo consumes card space, also watch the category 0x09 signals in
the output: 9.2 is a live remaining-recording-time hypothesis
(docs/protocol.md §5) and a per-photo storage tick would be the first lead
toward the remaining-photo-capacity state CLAUDE.md's storage gating needs.

Known passive limit — CONFIRMED for photo on both cameras (2026-07-27, 3
photo windows each on POCKET_6K_G2 v7.9 and POCKET_6K_PRO v8.6): a
body-triggered still produces NO photo-specific report at all; every photo
window contained only ambient telemetry (and, on the G2, connect-burst
spillover — see the operational note above). The trigger therefore needs
active probing (tools/control/discover_command.py --data-type VOID); this
sniffer remains useful for re-checking that result on new models/firmware
and for baseline windows around active probes. Full findings:
docs/photo_capture.md.

Override --actions for other attribution sessions, e.g. a photo while
recording, or per-codec photo windows:

    python tools/sniffers/sniffer_photo.py \\
        --actions idle_baseline,photo_while_recording,photo_after_stop

This tool only listens — it never writes to OUTGOING_CONTROL.

Usage:
    python tools/sniffers/sniffer_photo.py
    python tools/sniffers/sniffer_photo.py --model-key POCKET_6K_PRO --firmware v8.6
    python tools/sniffers/sniffer_photo.py --actions idle_baseline,photo_while_recording
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
    save_capture,
)

from bmd_ble.camera_controller import BMDCameraController  # noqa: E402
from bmd_ble.camera_profile import CameraProfile  # noqa: E402
from bmd_ble.scanner import scan_for_camera  # noqa: E402

DEFAULT_MODEL_KEY = "POCKET_6K_G2"
DEFAULT_FIRMWARE = "v7.9"
# idle_baseline first: it must capture the ambient floor before any photo
# has been taken, so a slow after-effect of a capture can't leak into it.
DEFAULT_ACTION_LABELS = [
    "idle_baseline",
    "photo_capture_1",
    "photo_capture_2",
    "photo_capture_3",
]


async def run(args: argparse.Namespace) -> int:
    labels = [label.strip() for label in args.actions.split(",") if label.strip()]
    if not labels:
        raise SystemExit("--actions must name at least one capture window label.")

    profile = CameraProfile.for_model(model_key=args.model_key, firmware=args.firmware)
    discovered = await scan_for_camera(profile.ble_name, timeout=args.timeout)
    cam = BMDCameraController(discovered=discovered, profile=profile)

    await cam.connect()
    try:
        session = await run_capture_windows(cam, labels)
        for window in session.windows:
            print_window_summary(window)
        saved_path = save_capture(args.model_key, args.firmware, session)
        print(f"\nCapture saved to: {saved_path}")
    finally:
        await cam.disconnect()

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sniff photo-capture BLE notifications from a camera while the "
            "operator takes stills on the body. Includes an idle baseline "
            "window to separate per-photo signals from ambient telemetry."
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
            "triggered action. Keep an idle window in the list so the "
            "ambient filter has contrast. Default: " + ",".join(DEFAULT_ACTION_LABELS)
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
