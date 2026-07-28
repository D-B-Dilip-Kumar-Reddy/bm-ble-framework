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

UPDATE (2026-07-23/24): real-hardware evidence ruled the `--data-type`
hypothesis out for `POCKET_6K_PRO v8.6`'s ProRes/4K DCI gap — `--data-type
INT16` with `--listen-seconds 8` produced zero fresh confirming reports,
the same signature already established for `0x82` (see docs/settings.md
§16). The write-byte axis is exhausted; see VIDEO_FORMAT TRAILING ELEMENTS
below for what's left.

VIDEO_FORMAT TRAILING ELEMENTS (`--video-format-extra`, added 2026-07-24,
see docs/settings.md §16): `video_format`'s five-element payload is
`[fps_int, m_rate, dimension_enum, extra1, extra2]` — every capture on
either camera so far shows `extra1`/`extra2` as `0, 0` (hypothesis: the
official spec's `interlaced`/`colorspace` video-mode elements, both zero
for progressive YUV), and `encode_video_format` has always hardcoded them.
With the `dimension_enum` search exhausted (`0x00`-`0x1F`, no ProRes/4K
DCI match) and the `recording_format` data-type hypothesis ruled out
above, these two unexplored bytes are the last untried lead from the
original candidate list. `--video-format-extra E1 E2` (accepts `0x..` hex
or decimal) overrides `build_command()`'s `extra1`/`extra2` for a
`video_format` send, leaving every other packet family untouched.
Default: unset, uses `(0, 0)` unchanged, so every existing invocation
stays byte-for-byte identical to before this flag existed. Like
`--dimension-enum` and `--data-type`, this is discovery-grade probing, not
a value known to mean anything yet — watch for a resulting
`recording_format`/`codec_quality` report matching the target instead of
guessing from the on-screen display, per the same caveat `send_settings_
command.py`'s other probe flags already carry:

    # Does a nonzero extra1 unlock ProRes/4K DCI where dimension_enum and
    # data_type alone couldn't?
    python tools/control/send_settings_command.py \\
        --model-key POCKET_6K_PRO --firmware v8.6 \\
        --packet video_format --resolution UHD --codec ProRes --fps 25 \\
        --video-format-extra 1 0

The override is recorded in the send's label (and so in the saved capture
JSON), e.g. `extra=(1,0) override; profile default (0,0)`, matching how
`--data-type`'s override is recorded.

UPDATE (2026-07-24): real-hardware evidence found no support for this
hypothesis either. Four `(extra1, extra2)` pairs tried against
`(UHD, ProRes, 25fps)`: `(1, 0)` confirmed 2/2 but still landed UHD, not
4K DCI; `(2, 0)`, `(0, 1)`, and `(1, 1)` were each silently rejected — the
same signature invalid `dimension_enum` candidates showed, not
`recording_format`'s "accepted but unconfirmed" one (see docs/settings.md
§16). All three original candidate hypotheses (dimension_enum sweep,
data_type retry, trailing elements) are now exhausted; a full-channel
decode of the passive-capture evidence found no hidden correlate either.
See OPERATION OVERRIDE below for what's left to try.

OPERATION OVERRIDE (`--operation`, added 2026-07-24, see docs/settings.md
§16): every write attempted so far — across all three exhausted
hypotheses above — used `Operation.ASSIGN` (packet header byte 7 = `0x00`,
`protocol/codec.py`). The header format documents a second write-capable
operation, `OFFSET` (`0x01`), never tried for any settings family on
either camera; its semantics for a resolution/format field are unknown.
`--operation NAME` (any `Operation` member name, e.g. `OFFSET`) overrides
`build_command()`'s operation byte for whichever `--packet` is selected —
generic across all three families, like the other override flags. Default:
unset, uses `Operation.ASSIGN` unchanged, so every existing invocation
stays byte-for-byte identical to before this flag existed. This is a
different axis than any hypothesis tried before it: those all varied a
*value* within an ASSIGN write; this varies the *operation* itself:

    # The one axis nothing has touched yet: does the PRO accept an OFFSET
    # write where ASSIGN never confirms?
    python tools/control/send_settings_command.py \\
        --model-key POCKET_6K_PRO --firmware v8.6 \\
        --packet recording_format --resolution "4K DCI" --fps 25 \\
        --operation OFFSET

Not yet tried on real hardware. As with the other probe flags, the
override is recorded in the send's label
(`operation=OFFSET(0x01 override; profile default ASSIGN/0x00)`), and a
generous `--listen-seconds` matters here too given this camera's
documented lens-burst timing confound.

UPDATE (2026-07-24): tried on real hardware — an absolute-target OFFSET
write (`--operation OFFSET` with the same values `--resolution "4K DCI"
--fps 25` would produce under ASSIGN) got zero response over a 10s
listen window: no `0x01/0x09` report, no report on any channel besides
the ambient `0x09/0x00` storage telemetry that free-runs regardless of
any write (see docs/settings.md §16). That is not itself proof `OFFSET`
is unsupported here — `docs/protocol.md` §4 documents `OFFSET`'s spec
meaning as "add the payload to the current value," so sending an
*absolute* target as an `OFFSET` is a category error, not a faithful
test of the hypothesis. See RAW PAYLOAD OVERRIDE below for the
delta-payload test this motivates.

RAW PAYLOAD OVERRIDE (`--raw-payload`, added 2026-07-24, see
docs/settings.md §16 and docs/protocol.md §4): every override above
changes one field of an otherwise profile-driven payload (a data-type
byte, two trailing elements, the operation byte) while still building
the rest of the payload from `--resolution`/`--codec`/`--fps` via the
profile's lookup tables. Testing `OFFSET`'s documented "add to current
value" semantics faithfully needs something none of those can do: a
*delta* payload, not an absolute target — e.g. retargeting
`recording_format` from UHD (3840x2160) to 4K DCI (4096x2160) via
`OFFSET` means sending a width delta of `4096-3840=256`, not the
absolute width `4096` that `--resolution "4K DCI"` would produce.
`--raw-payload VALUE [VALUE ...]` (accepts `0x..` hex or decimal per
element) bypasses `--resolution`/`--codec`/`--fps`/`--sensor-fps` and
the profile's lookup tables entirely, encoding the literal sequence as
the payload's elements in order — still reading category/parameter/
reserved from the profile's command block for the selected `--packet`
(protocol coordinates, not values under test) and still composing with
`--data-type`/`--operation`. It calls `encode_assign_elements`
(`protocol/codec.py`) directly — the same fully-generic encoder every
`encode_*` wrapper in `protocol/categories/settings.py` already
delegates to — so no protocol-layer changes were needed for this flag.
Default: unset; every existing invocation of this tool is unaffected.

    # The delta test OFFSET's documented semantics actually call for: a
    # +256 width delta (UHD -> 4K DCI), not an absolute target
    python tools/control/send_settings_command.py \\
        --model-key POCKET_6K_PRO --firmware v8.6 \\
        --packet recording_format --raw-payload 0 0 256 0 0 \\
        --operation OFFSET --listen-seconds 10

The five elements above match `recording_format`'s
`[fps_int, sensor_fps_int, width, height, frame_flags]` shape — `0` for
fps/sensor_fps/height/frame_flags (no change requested there), `256`
for the width delta. `--raw-payload` works with any `--packet` family;
its element count and per-index meaning are for the caller to get
right, per that packet's own payload shape — this flag does no
per-family validation, matching `--dimension-enum`'s and
`--video-format-extra`'s existing stance.

UPDATE (2026-07-24): tried on real hardware — the delta payload
(`--raw-payload 0 0 256 0 0 --operation OFFSET`, TX confirmed correct:
header byte 7 = `0x01`, payload decodes to `(0, 0, 256, 0, 0)`) got the
same zero-response signature as the absolute-payload test: no
`0x01/0x09` report anywhere in a full 10s window, only ambient `0x09`
telemetry and the usual connect-burst tail (see docs/settings.md §16).
This is a stronger result than the absolute-payload test — a `+256`
width delta from UHD (3840) lands exactly in-range at 4096, so the
"out-of-range absolute value" explanation for that earlier silence
doesn't apply here. Every hypothesis raised in this investigation
(`dimension_enum` sweep, `data_type` byte, `video_format` trailing
elements, full-channel passive decode, `OFFSET` absolute payload,
`OFFSET` delta payload) is now exhausted with no confirming echo for
the ProRes/4K DCI retarget. See docs/settings.md §16 for the full
write-up and the next diagnostic step under consideration (isolating
whether `OFFSET` is silently rejected for every category/parameter on
this camera, or specific to `recording_format`).

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
from bmd_ble.protocol.codec import Operation, encode_assign_elements  # noqa: E402
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


def resolve_video_format_extra(args: argparse.Namespace) -> tuple[int, int]:
    """The (extra1, extra2) trailing-element pair to encode with:
    `--video-format-extra E1 E2` if given (an explicit escape-hatch
    override — see the module docstring's VIDEO_FORMAT TRAILING ELEMENTS
    section), else `(0, 0)`, matching every real capture so far and every
    invocation of this tool before this flag existed."""
    if args.video_format_extra is not None:
        return tuple(args.video_format_extra)
    return (0, 0)


def _video_format_extra_suffix(extra1: int, extra2: int, args: argparse.Namespace) -> str:
    """Label suffix noting a `--video-format-extra` override — empty string
    when unused, so the label is byte-for-byte unchanged from before this
    flag existed. Mirrors `_data_type_override_suffix`'s evidence-visibility
    role: this flows into the saved capture JSON via `label`."""
    if args.video_format_extra is None:
        return ""
    return f" extra=({extra1},{extra2}) override; profile default (0,0)"


def resolve_operation(args: argparse.Namespace) -> Operation:
    """The operation byte to encode with: `--operation` if given (an explicit
    escape-hatch override — see the module docstring's OPERATION OVERRIDE
    section), else `Operation.ASSIGN`, matching every write this codebase has
    ever sent, across all three packet families and every invocation of this
    tool before this flag existed."""
    if args.operation is not None:
        return Operation[args.operation]
    return Operation.ASSIGN


def _operation_override_suffix(resolved: Operation, args: argparse.Namespace) -> str:
    """Label suffix noting an `--operation` override — empty string when
    unused, so the label is byte-for-byte unchanged from before this flag
    existed. Mirrors the other override suffixes' evidence-visibility role."""
    if args.operation is None:
        return ""
    return (
        f" operation={resolved.name}(0x{int(resolved):02X} override; profile default ASSIGN/0x00)"
    )


def _build_raw_payload_command(
    profile: CameraProfile, args: argparse.Namespace
) -> tuple[str, bytes]:
    """Build (label, command_bytes) directly from `--raw-payload`'s literal
    element values, bypassing the profile's codec/resolution/fps lookup
    tables entirely — see the module docstring's RAW PAYLOAD OVERRIDE
    section. Still reads category/parameter/reserved from the profile's
    command block for `--packet` (protocol coordinates, not values under
    test), and still composes with `--data-type`/`--operation`."""
    spec = profile.require_command(args.packet)
    resolved_data_type = resolve_data_type(spec, args)
    resolved_operation = resolve_operation(args)
    values = list(args.raw_payload)
    label = f"{args.packet} raw_payload={values}"
    label += _data_type_override_suffix(resolved_data_type, spec, args)
    label += _operation_override_suffix(resolved_operation, args)
    return label, encode_assign_elements(
        category=spec.category,
        parameter=spec.parameter,
        data_type=resolved_data_type,
        values=values,
        reserved=spec.reserved,
        operation=resolved_operation,
    )


def build_command(profile: CameraProfile, args: argparse.Namespace) -> tuple[str, bytes]:
    """Build (label, command_bytes) for the requested packet family, entirely
    from the profile — raises (via require_*) when the profile lacks the
    block or table entry, pointing at what to reverse-engineer first."""
    if args.raw_payload is not None:
        return _build_raw_payload_command(profile, args)

    if args.packet == "codec_quality":
        _require_flags(args, ("codec", "variant"))
        spec = profile.require_command("codec_quality")
        codec = profile.require_codec(args.codec, args.variant)
        resolved_data_type = resolve_data_type(spec, args)
        resolved_operation = resolve_operation(args)
        label = f"codec_quality {args.codec} {args.variant}"
        label += _data_type_override_suffix(resolved_data_type, spec, args)
        label += _operation_override_suffix(resolved_operation, args)
        return label, encode_codec_quality(
            category=spec.category,
            parameter=spec.parameter,
            data_type=resolved_data_type,
            codec_id=codec.id,
            variant_id=codec.variants[args.variant],
            reserved=spec.reserved,
            operation=resolved_operation,
        )

    if args.packet == "video_format":
        _require_flags(args, ("fps",))
        spec = profile.require_command("video_format")
        resolved_data_type = resolve_data_type(spec, args)
        resolved_operation = resolve_operation(args)
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
        extra1, extra2 = resolve_video_format_extra(args)
        label += _data_type_override_suffix(resolved_data_type, spec, args)
        label += _video_format_extra_suffix(extra1, extra2, args)
        label += _operation_override_suffix(resolved_operation, args)
        return label, encode_video_format(
            category=spec.category,
            parameter=spec.parameter,
            data_type=resolved_data_type,
            fps_int=fps.fps_int,
            m_rate=fps.m_rate,
            dimension_enum=dimension_enum,
            reserved=spec.reserved,
            extra1=extra1,
            extra2=extra2,
            operation=resolved_operation,
        )

    _require_flags(args, ("resolution", "fps"))
    spec = profile.require_command("recording_format")
    resolved_data_type = resolve_data_type(spec, args)
    resolved_operation = resolve_operation(args)
    resolution = profile.require_resolution(args.resolution)
    fps = profile.require_fps_mode(args.fps)
    sensor = profile.require_fps_mode(args.sensor_fps) if args.sensor_fps else fps
    label = f"recording_format {args.resolution} {args.fps}"
    label += _data_type_override_suffix(resolved_data_type, spec, args)
    label += _operation_override_suffix(resolved_operation, args)
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
        operation=resolved_operation,
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
    parser.add_argument("--firmware", required=True, help="Camera firmware, e.g. v8.6")
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
        "--video-format-extra",
        nargs=2,
        type=lambda s: int(s, 0),
        default=None,
        metavar=("EXTRA1", "EXTRA2"),
        help=(
            "video_format only: override the two trailing payload elements (accepts 0x.. "
            "hex or decimal) instead of the default 0, 0. Discovery-grade: probes whether "
            "these unexplained elements (see the module docstring's VIDEO_FORMAT TRAILING "
            "ELEMENTS section, docs/settings.md §16) are the missing piece for a gap a "
            "dimension_enum sweep can't close, e.g. --video-format-extra 1 0."
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
        "--operation",
        choices=[o.name for o in Operation],
        default=None,
        help=(
            "Override the wire operation byte (header byte 7) for this send instead of "
            "using Operation.ASSIGN — generic across all three --packet families. "
            "Discovery-grade: every write this codebase has ever sent used ASSIGN "
            "(0x00); the header format documents OFFSET (0x01) as the other "
            "write-capable operation, never tried (see the module docstring's "
            "OPERATION OVERRIDE section, docs/settings.md §16), e.g. --operation OFFSET. "
            "Default: unset, ASSIGN unchanged."
        ),
    )
    parser.add_argument(
        "--raw-payload",
        nargs="+",
        type=lambda s: int(s, 0),
        default=None,
        metavar="VALUE",
        help=(
            "Bypass --resolution/--codec/--fps/--sensor-fps and the profile's lookup "
            "tables entirely, encoding this literal sequence of per-element values "
            "(accepts 0x.. hex or decimal) as the payload for --packet, still using "
            "that packet's category/parameter/reserved from the profile. "
            "Discovery-grade: built for testing Operation.OFFSET's documented delta "
            "semantics (see the module docstring's RAW PAYLOAD OVERRIDE section, "
            "docs/protocol.md §4), e.g. --raw-payload 0 0 256 0 0 --operation OFFSET "
            "to request a +256 width delta (UHD -> 4K DCI) instead of an absolute "
            "target. Composes with --data-type/--operation. Default: unset, normal "
            "per-packet resolution unchanged."
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
