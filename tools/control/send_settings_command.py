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

CONNECT SETTLE (added 2026-07-20, see docs/settings.md §6): a just-connected
camera floods INCOMING_CONTROL with an initial info dump (recording state,
media/scene metadata, lens data, ISO, ...) that can take several seconds to
drain — the exact hazard `CameraSession.__aenter__` waits `connect_settle_s`
for (docs/session_and_verification.md). This tool did not wait, so its
first three real-hardware runs (2026-07-20) captured that burst instead of
a response to the write: none of the three showed the target
category/parameter, only unrelated initial-payload packets. Fixed by
waiting `--connect-settle-seconds` (default 6.0s, matching
`CameraSession`'s default) after connecting and before the send-and-capture
window opens.

REDUNDANT-WRITE PROBE (`--repeat`, added 2026-07-21, see docs/settings.md
§13): real-hardware evidence proved that `codec_quality`'s report only
fires on an *applied* change — requesting the (codec, variant) the camera
is already at produces no echo at all, not a slow one (docs/settings.md
§11; `CameraSession.set_codec_quality` now guards against it via
`last_known_codec_variant`). Whether `video_format` and `recording_format`
share that same silent-no-op behavior is still an open question — nobody
has captured it yet. `--repeat N` (default 1) sends the exact same command
bytes N times in one connected session, each into its own labeled capture
window, so a caller can put the camera in the target state with send 1 and
then deliberately probe the no-op case with send 2 onward:

    # Does recording_format echo on a redundant write, or go silent?
    python tools/control/send_settings_command.py \\
        --model-key POCKET_6K_G2 --firmware v7.9 \\
        --packet recording_format --resolution "4K DCI" --fps 25 --repeat 2

Read the two windows' summaries side by side: a normal echo on both means
that family always reports regardless of whether anything changed (no
no-op guard needed); `(none observed)` on the second window only
reproduces the `codec_quality` finding for this family too (a no-op guard
belongs there, mirroring `last_known_codec_variant`).

DATA_TYPE OVERRIDE (`--data-type`, added 2026-07-23, see docs/settings.md
§3/§4/§16): `recording_format` writes have always used wire data-type byte
`0x82` (`DataType.INT16_ARRAY`) — a CANDIDATE value transcribed from an
external reverse-engineering document, not part of the official BMD spec.
The camera's own REPORT packets for that exact category/parameter always
use the spec-official `0x02` (`DataType.INT16`) instead — a documented
discrepancy (§3), flagged as an open hypothesis since §4.2 ("if `0x82` is
rejected, try the write with `0x02`"). It didn't matter on
`POCKET_6K_G2 v7.9` — `0x82` was empirically confirmed accepted there
(§10) — but on `POCKET_6K_PRO v8.6`, a `recording_format` write
retargeting resolution to 4K DCI while ProRes is active never confirms at
all (§16), even though the target state is independently proven real.
`--data-type NAME` (any `DataType` member name, e.g. `INT16`) overrides
whichever `spec.data_type` `build_command()` would otherwise use for the
selected `--packet` — generic across all three families, not just
`recording_format`, since this discrepancy is a property of the wire byte
itself, not of one packet family. Default: unset, uses the profile's own
value unchanged, so every existing invocation of this tool is byte-for-byte
identical to before this flag existed. `INT16` and `INT16_ARRAY` map to the
identical struct format and byte width in `protocol/types.py`, so this
changes only packet header byte 6, never the payload encoding or length —
a discovery-grade experiment on what the camera *accepts*, not a
payload/shape change. The override is recorded in the send's label (and
so in the saved capture JSON), e.g. `data_type=INT16(0x02 override;
profile default INT16_ARRAY/0x82)`, so a later reviewer can tell at a
glance which captures used a non-profile byte:

    # §16's open hypothesis: does the PRO accept the retarget with 0x02
    # instead of the claimed 0x82?
    python tools/control/send_settings_command.py \\
        --model-key POCKET_6K_PRO --firmware v8.6 \\
        --packet recording_format --resolution "4K DCI" --fps 25 \\
        --data-type INT16

If real-hardware evidence shows the camera accepts and confirms this,
that's grounds for a *separate*, evidence-gated follow-up — promoting
`payloads/models/POCKET_6K_PRO_v8.6.json`'s
`commands.recording_format.data_type` from `INT16_ARRAY` to `INT16` with
updated provenance. This flag only makes the experiment possible; it does
not itself change any profile.

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
from bmd_ble.camera_profile import CameraProfile, CommandSpec  # noqa: E402
from bmd_ble.protocol.categories.settings import (  # noqa: E402
    encode_codec_quality,
    encode_recording_format,
    encode_video_format,
)
from bmd_ble.protocol.types import DataType  # noqa: E402
from bmd_ble.scanner import scan_for_camera  # noqa: E402

PACKET_CHOICES = ("codec_quality", "video_format", "recording_format")


def _require_flags(args: argparse.Namespace, needed: tuple[str, ...]) -> None:
    missing = [flag for flag in needed if getattr(args, flag) is None]
    if missing:
        flags = ", ".join(f"--{flag.replace('_', '-')}" for flag in missing)
        raise SystemExit(f"--packet {args.packet} requires {flags}")


def resolve_data_type(spec: CommandSpec, args: argparse.Namespace) -> DataType:
    """The data_type byte to encode with: `--data-type` if given (an explicit
    escape-hatch override for one-off discovery-grade sends — see the module
    docstring's DATA_TYPE OVERRIDE section), else the profile's own
    `spec.data_type` unchanged. Generic across all three packet families
    because every CommandSpec carries this field identically — the
    CANDIDATE-vs-spec wire-byte discrepancy this probes (docs/settings.md §3)
    isn't specific to any one family."""
    if args.data_type is not None:
        return DataType[args.data_type]
    return spec.data_type


def _data_type_override_suffix(
    resolved: DataType, spec: CommandSpec, args: argparse.Namespace
) -> str:
    """Label suffix noting a `--data-type` override, so it's visible both on
    the console and in the saved capture JSON (`label` flows straight into
    `run_send_and_capture` -> `save_capture`) — empty string when unused, so
    the label is byte-for-byte unchanged from before this flag existed."""
    if args.data_type is None:
        return ""
    return (
        f" data_type={resolved.name}(0x{int(resolved):02X} override; "
        f"profile default {spec.data_type.name}/0x{int(spec.data_type):02X})"
    )


def build_command(profile: CameraProfile, args: argparse.Namespace) -> tuple[str, bytes]:
    """Build (label, command_bytes) for the requested packet family, entirely
    from the profile — raises (via require_*) when the profile lacks the
    block or table entry, pointing at what to reverse-engineer first."""
    if args.packet == "codec_quality":
        _require_flags(args, ("codec", "variant"))
        spec = profile.require_command("codec_quality")
        codec = profile.require_codec(args.codec, args.variant)
        resolved_data_type = resolve_data_type(spec, args)
        label = f"codec_quality {args.codec} {args.variant}"
        label += _data_type_override_suffix(resolved_data_type, spec, args)
        return label, encode_codec_quality(
            category=spec.category,
            parameter=spec.parameter,
            data_type=resolved_data_type,
            codec_id=codec.id,
            variant_id=codec.variants[args.variant],
            reserved=spec.reserved,
        )

    if args.packet == "video_format":
        _require_flags(args, ("fps",))
        spec = profile.require_command("video_format")
        resolved_data_type = resolve_data_type(spec, args)
        fps = profile.require_fps_mode(args.fps)
        if args.dimension_enum is not None:
            # Probe mode: send a candidate enum that is NOT in the profile
            # yet. Dimension enums never appear in notifications (confirmed
            # by the 2026-07-20 passive capture), so an active probe like
            # this is the only way to map a missing (resolution, codec)
            # enum — e.g. 4K DCI ProRes. The operator watches what the
            # camera switches to; the 1/9 report in the capture shows the
            # resulting width/height.
            dimension_enum = args.dimension_enum
            label = f"video_format probe enum=0x{dimension_enum:02X} {args.fps}"
        else:
            _require_flags(args, ("resolution", "codec"))
            resolution = profile.require_resolution(args.resolution)
            profile.require_codec(args.codec)
            dimension_enum = resolution.dimension_enums.get(args.codec)
            if dimension_enum is None:
                raise SystemExit(
                    f"dimension_enum for '{args.resolution}' under '{args.codec}' is not in "
                    f"the profile — enums never appear in notifications, so probe candidates "
                    f"actively with --dimension-enum (see docs/settings.md)."
                )
            label = f"video_format {args.resolution} {args.codec} {args.fps}"
        label += _data_type_override_suffix(resolved_data_type, spec, args)
        return label, encode_video_format(
            category=spec.category,
            parameter=spec.parameter,
            data_type=resolved_data_type,
            fps_int=fps.fps_int,
            m_rate=fps.m_rate,
            dimension_enum=dimension_enum,
            reserved=spec.reserved,
        )

    _require_flags(args, ("resolution", "fps"))
    spec = profile.require_command("recording_format")
    resolved_data_type = resolve_data_type(spec, args)
    resolution = profile.require_resolution(args.resolution)
    fps = profile.require_fps_mode(args.fps)
    sensor = profile.require_fps_mode(args.sensor_fps) if args.sensor_fps else fps
    label = f"recording_format {args.resolution} {args.fps}"
    label += _data_type_override_suffix(resolved_data_type, spec, args)
    return label, encode_recording_format(
        category=spec.category,
        parameter=spec.parameter,
        data_type=resolved_data_type,
        fps_int=fps.fps_int,
        sensor_fps_int=sensor.fps_int,
        width=resolution.width,
        height=resolution.height,
        frame_flags=fps.frame_flags,
        reserved=spec.reserved,
    )


async def confirm_send(label: str, command: bytes, model_key: str, *, repeat: int = 1) -> bool:
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
    if repeat > 1:
        print(
            f"\n--repeat {repeat}: this exact command will be sent {repeat} times in a row "
            "(a redundant-write echo probe — see the module docstring)."
        )
    loop = asyncio.get_running_loop()
    answer = await loop.run_in_executor(None, input, "Type 'yes' to proceed: ")
    return answer.strip().lower() == "yes"


def build_repeated_actions(label: str, command: bytes, repeat: int) -> list[tuple[str, bytes]]:
    """`repeat` copies of `(label, command)`, suffixed with a send index when
    `repeat > 1` so each capture window (`run_send_and_capture`) is
    individually identifiable — see the module docstring's REDUNDANT-WRITE
    PROBE section. `repeat == 1` returns the label unchanged, matching this
    tool's prior single-send behavior exactly."""
    if repeat == 1:
        return [(label, command)]
    return [(f"{label} (send {i + 1}/{repeat})", command) for i in range(repeat)]


async def run(args: argparse.Namespace) -> int:
    profile = CameraProfile.for_model(model_key=args.model_key, firmware=args.firmware)
    label, command = build_command(profile, args)

    if not await confirm_send(label, command, args.model_key, repeat=args.repeat):
        print("Aborted before any write.")
        return 1

    discovered = await scan_for_camera(profile.ble_name, timeout=args.timeout)
    cam = BMDCameraController(discovered=discovered, profile=profile)

    await cam.connect()
    try:
        # See the module docstring's "CONNECT SETTLE" note: let the
        # post-connect initial-payload burst fully drain before opening the
        # capture window, so it isn't mistaken for a response to this write.
        print(f"Waiting {args.connect_settle_seconds}s for the initial payload burst to settle…")
        await asyncio.sleep(args.connect_settle_seconds)

        actions = build_repeated_actions(label, command, args.repeat)
        session = await run_send_and_capture(cam, actions, listen_seconds=args.listen_seconds)
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
        "--dimension-enum",
        type=lambda s: int(s, 0),
        default=None,
        help=(
            "video_format only: send this raw dimension_enum byte instead of looking one up "
            "from the profile (accepts 0x.. hex). Discovery-grade: use it to map enums the "
            "profile lacks (e.g. the 4K DCI ProRes enum) — enums never appear in "
            "notifications, so an active probe is the only way. Watch the camera and note "
            "what it switches to; then add the confirmed enum to the profile's resolutions "
            "table."
        ),
    )
    parser.add_argument(
        "--data-type",
        choices=[t.name for t in DataType],
        default=None,
        help=(
            "Override the wire data_type byte for this send instead of using the "
            "profile's own value — generic across all three --packet families. "
            "Discovery-grade: use it to probe the CANDIDATE-vs-spec data-type-byte "
            "discrepancy (see the module docstring's DATA_TYPE OVERRIDE section, "
            "docs/settings.md §3/§4/§16), e.g. --data-type INT16 to try 0x02 instead "
            "of the claimed write byte 0x82. Default: unset, profile's value unchanged."
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "Send the exact same command this many times in one connected session, each "
            "into its own labeled capture window — the redundant-write echo probe (see the "
            "module docstring). Default: 1 (unchanged single-send behavior)."
        ),
    )
    parser.add_argument(
        "--listen-seconds",
        type=float,
        default=3.0,
        help="Seconds to listen for a response after the command. Default: 3.0",
    )
    parser.add_argument(
        "--connect-settle-seconds",
        type=float,
        default=6.0,
        help=(
            "Seconds to wait after connecting, before sending, for the camera's "
            "post-connect initial-payload burst to drain (matches CameraSession's "
            "connect_settle_s default) — see the module docstring's CONNECT SETTLE note. "
            "Default: 6.0"
        ),
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0, help="BLE scan timeout in seconds. Default: 15.0"
    )
    return parser.parse_args()


if __name__ == "__main__":
    configure_console_logging()
    raise SystemExit(asyncio.run(run(parse_args())))
