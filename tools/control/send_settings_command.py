"""
tools/control/send_settings_command.py
=======================================
Actively sends one of the settings-family commands (codec_quality,
video_format, recording_format — see docs/settings.md) to a real camera and
captures the response. This WILL change the camera's codec / quality /
resolution / FPS settings — note the camera's current settings first so you
can restore them.

Command bytes are built from the profile's `commands.*` blocks plus the
`codecs`/`resolutions`/`fps_modes` lookup tables (never hardcoded). Because
those blocks are CANDIDATE — transcribed from an external
reverse-engineering document, not yet re-verified by this repo's tooling —
this tool gates the write behind a typed 'yes' after showing the exact TX
bytes, like tools/control/discover_command.py and unlike
send_record_command.py (whose command family is VERIFIED).

The captured response is the evidence that either verifies the family
(operator watched the camera change + echo captured -> update the profile's
provenance) or falsifies it. Two runs make the doc's central claim testable:

  # Claim: codec_quality alone does NOT switch BRAW -> ProRes
  python tools/control/send_settings_command.py \\
      --model-key POCKET_6K_G2 --firmware v7.9 \\
      --packet codec_quality --codec ProRes --variant HQ

  # Claim: video_format's dimension_enum DOES switch the codec family
  python tools/control/send_settings_command.py \\
      --model-key POCKET_6K_G2 --firmware v7.9 \\
      --packet video_format --resolution UHD --codec ProRes --fps 25

Watch the camera body after each send — the operator's eyes, not the echo,
are ground truth for what changed (same stance as docs/command_discovery.md).

Usage:
    python tools/control/send_settings_command.py --model-key POCKET_6K_G2 --firmware v7.9 \\
        --packet recording_format --resolution "4K DCI" --fps 25
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from capture import (  # noqa: E402
    configure_console_logging,
    run_send_and_capture,
    save_capture,
)

from bmd_ble.camera_controller import BMDCameraController  # noqa: E402
from bmd_ble.camera_profile import CameraProfile  # noqa: E402
from bmd_ble.protocol.categories.settings import (  # noqa: E402
    encode_codec_quality,
    encode_recording_format,
    encode_video_format,
)
from bmd_ble.scanner import scan_for_camera  # noqa: E402

PACKET_CHOICES = ("codec_quality", "video_format", "recording_format")


def _require_flags(args: argparse.Namespace, needed: tuple[str, ...]) -> None:
    missing = [flag for flag in needed if getattr(args, flag) is None]
    if missing:
        flags = ", ".join(f"--{flag.replace('_', '-')}" for flag in missing)
        raise SystemExit(f"--packet {args.packet} requires {flags}")


def build_command(profile: CameraProfile, args: argparse.Namespace) -> tuple[str, bytes]:
    """Build (label, command_bytes) for the requested packet family, entirely
    from the profile — raises (via require_*) when the profile lacks the
    block or table entry, pointing at what to reverse-engineer first."""
    if args.packet == "codec_quality":
        _require_flags(args, ("codec", "variant"))
        spec = profile.require_command("codec_quality")
        codec = profile.require_codec(args.codec, args.variant)
        label = f"codec_quality {args.codec} {args.variant}"
        return label, encode_codec_quality(
            category=spec.category,
            parameter=spec.parameter,
            data_type=spec.data_type,
            codec_id=codec.id,
            variant_id=codec.variants[args.variant],
            reserved=spec.reserved,
        )

    if args.packet == "video_format":
        _require_flags(args, ("resolution", "codec", "fps"))
        spec = profile.require_command("video_format")
        resolution = profile.require_resolution(args.resolution)
        profile.require_codec(args.codec)
        fps = profile.require_fps_mode(args.fps)
        dimension_enum = resolution.dimension_enums.get(args.codec)
        if dimension_enum is None:
            raise SystemExit(
                f"dimension_enum for '{args.resolution}' under '{args.codec}' is not in the "
                f"profile — capture it first (tools/sniffers/sniffer_settings.py, see "
                f"docs/settings.md)."
            )
        label = f"video_format {args.resolution} {args.codec} {args.fps}"
        return label, encode_video_format(
            category=spec.category,
            parameter=spec.parameter,
            data_type=spec.data_type,
            fps_int=fps.fps_int,
            m_rate=fps.m_rate,
            dimension_enum=dimension_enum,
            reserved=spec.reserved,
        )

    _require_flags(args, ("resolution", "fps"))
    spec = profile.require_command("recording_format")
    resolution = profile.require_resolution(args.resolution)
    fps = profile.require_fps_mode(args.fps)
    sensor = profile.require_fps_mode(args.sensor_fps) if args.sensor_fps else fps
    label = f"recording_format {args.resolution} {args.fps}"
    return label, encode_recording_format(
        category=spec.category,
        parameter=spec.parameter,
        data_type=spec.data_type,
        fps_int=fps.fps_int,
        sensor_fps_int=sensor.fps_int,
        width=resolution.width,
        height=resolution.height,
        frame_flags=fps.frame_flags,
        reserved=spec.reserved,
    )


async def confirm_send(label: str, command: bytes, model_key: str) -> bool:
    """Typed-yes gate before the write — this family is CANDIDATE, so the
    bytes have never been confirmed by this repo's tooling on any camera."""
    tx = " ".join(f"{b:02X}" for b in command)
    print(f"\nAbout to send to {model_key}:")
    print(f"  {label}  ->  TX: {tx}")
    print(
        "\nThis is a CANDIDATE command (see the profile block's provenance) and WILL"
        "\nchange the camera's settings. Note the current settings so you can restore"
        "\nthem, keep the camera in view, and watch what it actually does."
    )
    loop = asyncio.get_running_loop()
    answer = await loop.run_in_executor(None, input, "Type 'yes' to proceed: ")
    return answer.strip().lower() == "yes"


async def run(args: argparse.Namespace) -> int:
    profile = CameraProfile.for_model(model_key=args.model_key, firmware=args.firmware)
    label, command = build_command(profile, args)

    if not await confirm_send(label, command, args.model_key):
        print("Aborted before any write.")
        return 1

    discovered = await scan_for_camera(profile.ble_name, timeout=args.timeout)
    cam = BMDCameraController(discovered=discovered, profile=profile)

    await cam.connect()
    try:
        session = await run_send_and_capture(
            cam, [(label, command)], listen_seconds=args.listen_seconds
        )
        saved_path = save_capture(args.model_key, args.firmware, session)
        print(f"\nCapture saved to: {saved_path}")
        print(
            "\nDid the camera physically change as requested? If yes AND the capture "
            "shows a matching report, update the profile block's provenance "
            "(docs/settings.md's runbook); if the camera did nothing, that finding "
            "belongs in the provenance notes too."
        )
    finally:
        await cam.disconnect()

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Actively send a CANDIDATE settings command (codec_quality / video_format / "
            "recording_format) built from the profile, and capture the response. This "
            "WILL change camera settings. No defaults for --model-key/--firmware — be "
            "explicit about which camera you are changing."
        )
    )
    parser.add_argument("--model-key", required=True, help="Camera model key, e.g. POCKET_6K_G2")
    parser.add_argument("--firmware", required=True, help="Camera firmware, e.g. v7.9")
    parser.add_argument(
        "--packet",
        required=True,
        choices=PACKET_CHOICES,
        help="Which settings packet family to send (see docs/settings.md).",
    )
    parser.add_argument("--codec", help="Codec name from the profile's codecs table, e.g. BRAW")
    parser.add_argument(
        "--variant", help="Quality variant from the codec's variants table, e.g. 5:1"
    )
    parser.add_argument("--resolution", help='Resolution label from the profile, e.g. "4K DCI"')
    parser.add_argument("--fps", help="FPS label from the profile's fps_modes table, e.g. 25")
    parser.add_argument(
        "--sensor-fps",
        help="Optional off-speed sensor FPS label (recording_format only). Defaults to --fps.",
    )
    parser.add_argument(
        "--listen-seconds",
        type=float,
        default=3.0,
        help="Seconds to listen for a response after the command. Default: 3.0",
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0, help="BLE scan timeout in seconds. Default: 15.0"
    )
    return parser.parse_args()


if __name__ == "__main__":
    configure_console_logging()
    raise SystemExit(asyncio.run(run(parse_args())))
