"""
tools/sniffers/sniffer_sensor_area.py
======================================
Passive sniffer for the ProRes "Sensor Area" setting — see
docs/photo_capture.md §8 and §10.

No BLE coordinates are known for this setting yet — that is exactly what
this sniffer exists to discover. It is not the same thing as
commands.video_format's dimension_enum: that table only offers ProRes at
"UHD" and "HD" (the *video recording* resolutions), while the operator has
reported (docs/photo_capture.md §8) that a ProRes *still photo*'s pixel
dimensions are instead decided by one of three sensor-area readouts —
2.8K, 5.7K, or 6K — unrelated to whichever UHD/HD video resolution is
active at the time. Whether "Sensor Area" is a distinct camera-menu
setting with its own BLE parameter, or turns out to secretly be the same
dimension_enum mechanism observed from a different angle, is an open
question this capture is meant to help answer — do not assume either way
before reading the result.

Precondition: the camera must already be set to ProRes before running this
sniffer (set via CameraSession.set_camera_format or the body menu) — the
operator-reported finding this investigates is explicitly ProRes-only; in
BRAW, still dimensions instead follow the ordinary recording resolution
(docs/photo_capture.md §8.1), which is already fully modeled by the
existing resolutions/dimension_enum tables and needs no new sniffing.

Connects to the camera, then runs one interactive capture window per action
label. The operator changes "Sensor Area" to the labeled value on the
camera body's menu between two Enter presses; the tool only listens. For
each window it prints every (characteristic, category, parameter) triple
observed on INCOMING_CONTROL / CAMERA_STATUS and saves the full decoded
capture to tools/captures/.

Default windows and why:

  idle_baseline      Operator does NOTHING — captures the ambient telemetry
                      floor before any sensor-area change, so
                      tools/common/discovery.py's seed_triples_from_capture
                      ambient filter has the contrast it needs (the same
                      reason sniffer_photo.py leads with this window).
  sensor_area_2_8k    One window per concrete sensor-area value, so each
  sensor_area_5_7k    captured packet is unambiguously attributable to one
  sensor_area_6k      setting — the same value-mapping pattern
                      sniffer_settings.py's --actions uses for resolution/
                      codec/quality/fps.

Operational note (carried over from sniffer_photo.py's real-hardware
findings, docs/photo_capture.md §5.2): both cameras drain a large
post-connect state-report burst over the indication channel at a
throttled ~180ms cadence lasting 10s or more. Open the idle_baseline
window only AFTER that burst has finished — wait until notifications slow
to the ~1/s ambient cadence — or the burst contaminates the baseline (and
can spill into the first real window, as it did for sniffer_photo.py's
first G2 run).

If this capture shows a reporting (category, parameter) for a sensor-area
change, seed tools/control/discover_command.py --from-capture with it to
find the write coordinates, the same workflow used for recording
(docs/command_discovery.md). If it shows nothing — matching the
still-capture trigger's own null result (docs/photo_capture.md §5) — that
is itself a finding worth recording, not a failure.

This tool only listens — it never writes to OUTGOING_CONTROL.

Usage:
    python tools/sniffers/sniffer_sensor_area.py
    python tools/sniffers/sniffer_sensor_area.py --model-key POCKET_6K_PRO --firmware v8.6
    python tools/sniffers/sniffer_sensor_area.py --actions idle_baseline,sensor_area_2_8k
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
# idle_baseline first: it must capture the ambient floor before any
# sensor-area change has been made, so a slow after-effect can't leak in.
DEFAULT_ACTION_LABELS = [
    "idle_baseline",
    "sensor_area_2_8k",
    "sensor_area_5_7k",
    "sensor_area_6k",
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
            "Sniff BLE notifications from a camera while the operator changes "
            "ProRes's 'Sensor Area' setting on the body. Includes an idle "
            "baseline window to separate per-change signals from ambient "
            "telemetry. Camera must already be set to ProRes before running."
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
            "triggered change. Keep an idle window in the list so the "
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
