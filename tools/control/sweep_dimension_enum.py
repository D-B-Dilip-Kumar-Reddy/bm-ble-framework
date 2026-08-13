"""
tools/control/sweep_dimension_enum.py
======================================
Exhaustive `video_format` `dimension_enum` sweep — sends a range of candidate
`dimension_enum` bytes to a real camera, one connected session, and decodes
each result straight off the wire rather than relying on the operator's eyes
on the body's screen.

WHY THIS EXISTS
----------------
`tools/control/send_settings_command.py --packet video_format --dimension-enum
0x..` already sends one candidate at a time (see docs/ble/settings.md §7-§8 for
the workflow it supports), but each invocation re-scans and reconnects
(~15-20s of overhead) and leaves match detection to the operator reading the
resulting resolution off the camera body. That's unreliable on some cameras:
`POCKET_6K_PRO v8.6`'s on-screen display does NOT live-update after a
video_format write until the camera is power-cycled, even though the write
demonstrably takes effect (docs/ble/settings.md §15) — so an operator watching
the screen during a sweep would see nothing change on every single candidate,
match or not.

This tool instead connects once, sends every candidate in the sweep, and
decodes the resulting `recording_format` (mode-notify) and `codec_quality`
reports straight from the capture — the same wire evidence
`commands.video_format`'s provenance already treats as ground truth
elsewhere in this codebase. Give it `--target-resolution` (and optionally
`--target-codec`) and it flags a match automatically: no on-screen check
needed, no guessing.

Motivating case (see docs/ble/settings.md §16): `POCKET_6K_PRO v8.6` has no known
`dimension_enum` for ProRes/4K DCI, and `set_recording_format`'s two-step
proxy workaround that closes the equivalent gap on the G2 does not work here.
A same-day passive capture confirmed the camera genuinely holds and reports
ProRes/4K DCI when reached by hand through the body menu — so the state is
real, and an exhaustive sweep across untried `dimension_enum` values is the
most promising way to find whatever value (if any) reaches it directly.

    python tools/control/sweep_dimension_enum.py \\
        --model-key POCKET_6K_PRO --firmware v8.6 \\
        --fps 25 --target-resolution "4K DCI" --target-codec ProRes

With no `--enums`/`--range`, the default candidate range is `0x00`-`0x16`
(matching the range the G2's own exhaustive 4K DCI/ProRes search covered,
docs/ble/settings.md §7-§8), minus every `dimension_enum` value already present
in the profile's `resolutions` table (no point resending a value whose
target is already known) — pass `--include-known` to sweep those too.

Every candidate is a real write to a CANDIDATE command family, so this is
typed-yes gated once for the whole sweep (like
tools/control/discover_command.py), not per candidate — reading the sweep
plan before confirming is the operator's chance to trim it down with
`--enums`/`--range`/`--include-known` first.

STALE-MATCH GUARD (added 2026-07-27, real false positive on
`POCKET_6K_PRO v8.6` — see docs/ble/photo_capture.md §10.1's dimension_enum-
aliasing hunt). `is_match` only checks whether a candidate's decoded state
equals the target — it has no way to know whether that state was actually
*caused* by this candidate, versus already being true beforehand (an
invalid/no-op enum produces no real write, but the camera still reflects
whatever it was already at — the same "report isn't an ack, it's a state
reflection" mechanism documented in `docs/ble/settings.md` §7). Candidate
`0x00` demonstrated this directly: an apparent MATCH against ProRes/HD in
one run, and a completely different, non-matching resolution in an
otherwise-identical rerun — both times exactly reproducing whatever the
camera held immediately *before* `0x00` was sent, not a result of `0x00`
itself. The sweep now tracks the last confirmed `(width, height, flags)`
state across candidates (carried forward through silent ones) and flags
any MATCH that is byte-identical to it as a **possible stale match**,
both inline and in the final summary — real matches should still show up
clean unless the immediately preceding candidate happened to reach the
same state by coincidence, which a repeat/interleaved run resolves.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from capture import (  # noqa: E402
    CaptureSession,
    CaptureWindow,
    DecodedNotification,
    configure_console_logging,
    run_send_and_capture,
    save_capture,
)
from discovery import INCOMING_CONTROL_NAME  # noqa: E402

from bmd_camera.ble.camera_controller import BMDCameraController  # noqa: E402
from bmd_camera.ble.protocol.categories.settings import (  # noqa: E402
    RecordingFormat,
    decode_codec_quality,
    decode_recording_format,
    encode_video_format,
)
from bmd_camera.ble.protocol.types import DataType  # noqa: E402
from bmd_camera.ble.scanner import scan_for_camera  # noqa: E402
from bmd_camera.camera_profile import CameraProfile  # noqa: E402

DEFAULT_RANGE = (0x00, 0x16)


def parse_int_list(raw: str, flag: str) -> list[int]:
    try:
        return [int(part, 0) for part in raw.split(",") if part.strip() != ""]
    except ValueError as exc:
        raise SystemExit(f"{flag}: expected comma-separated integers, got {raw!r}") from exc


def known_dimension_enums(profile: CameraProfile) -> set[int]:
    """Every `dimension_enum` value already recorded in the profile's
    `resolutions` table, across every codec — candidates already answered,
    not worth resending by default."""
    return {
        enum_value
        for resolution in profile.resolutions.values()
        for enum_value in resolution.dimension_enums.values()
    }


def compute_candidates(args: argparse.Namespace, profile: CameraProfile) -> list[int]:
    if args.enums:
        candidates = parse_int_list(args.enums, "--enums")
    else:
        start, end = args.range if args.range else DEFAULT_RANGE
        candidates = list(range(start, end + 1))

    if not args.include_known:
        known = known_dimension_enums(profile)
        candidates = [c for c in candidates if c not in known]

    # De-dup while preserving the caller's ordering.
    seen: set[int] = set()
    ordered: list[int] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def latest_decoded(
    window: CaptureWindow, *, category: int, parameter: int
) -> DecodedNotification | None:
    """The most recent cleanly-decoded INCOMING_CONTROL notification in this
    window matching (category, parameter) — a window can hold a stale
    duplicate of the prior state ahead of a fresh one, so the latest, not
    the first, reflects what the write actually produced."""
    match: DecodedNotification | None = None
    for notification in window.notifications:
        if notification.characteristic_name != INCOMING_CONTROL_NAME:
            continue
        if notification.decode_error is not None:
            continue
        if notification.category != category or notification.parameter != parameter:
            continue
        match = notification
    return match


class ResultDecoder:
    """Decodes each sweep window's `recording_format`/`codec_quality`
    reports, best-effort: a profile that hasn't reverse-engineered those
    command blocks yet (early Phase 3) still gets raw-hex capture evidence,
    just without automated match detection."""

    def __init__(self, profile: CameraProfile) -> None:
        try:
            rf_spec = profile.require_command("recording_format")
            cq_spec = profile.require_command("codec_quality")
        except ValueError as exc:
            print(
                f"NOTE: automated match decoding disabled ({exc}) — showing raw hex only. "
                "Reverse-engineer recording_format/codec_quality first (CLAUDE.md Phase 3, "
                "step 9) for automated matching."
            )
            self._rf = None
            self._cq = None
        else:
            self._rf = (rf_spec.category, rf_spec.parameter)
            self._cq = (cq_spec.category, cq_spec.parameter)

    def decode(self, window: CaptureWindow) -> tuple[RecordingFormat | None, tuple | None]:
        recording_format = None
        codec_quality = None
        if self._rf is not None:
            notification = latest_decoded(window, category=self._rf[0], parameter=self._rf[1])
            if notification is not None:
                recording_format = decode_recording_format(
                    bytes.fromhex(notification.payload_hex),
                    DataType[notification.data_type],
                )
        if self._cq is not None:
            notification = latest_decoded(window, category=self._cq[0], parameter=self._cq[1])
            if notification is not None:
                codec_quality = decode_codec_quality(
                    bytes.fromhex(notification.payload_hex),
                    DataType[notification.data_type],
                )
        return recording_format, codec_quality


def describe_result(
    recording_format: RecordingFormat | None, codec_quality: tuple[int, int] | None
) -> str:
    parts = []
    if recording_format is not None:
        parts.append(
            f"recording_format: fps={recording_format.fps_int} "
            f"{recording_format.width}x{recording_format.height} "
            f"flags=0x{recording_format.frame_flags:02X}"
        )
    if codec_quality is not None:
        codec_id, variant_id = codec_quality
        parts.append(f"codec_quality: codec_id={codec_id} variant_id={variant_id}")
    return "; ".join(parts) if parts else "(no recording_format/codec_quality report)"


def is_match(
    recording_format: RecordingFormat | None,
    codec_quality: tuple[int, int] | None,
    *,
    target_width: int | None,
    target_height: int | None,
    target_codec_id: int | None,
) -> bool:
    if target_width is None or target_height is None:
        return False
    if recording_format is None:
        return False
    if recording_format.width != target_width or recording_format.height != target_height:
        return False
    if target_codec_id is None:
        return True
    return codec_quality is not None and codec_quality[0] == target_codec_id


def recording_format_state(
    recording_format: RecordingFormat | None,
) -> tuple[int, int, int] | None:
    """(width, height, flags) fingerprint used to detect a stale match — a
    report whose content is identical to whatever the camera was already
    reporting before this candidate was sent, i.e. leftover state rather
    than something this candidate itself caused. `None` when nothing
    decoded (a genuinely silent candidate carries no state to compare)."""
    if recording_format is None:
        return None
    return (recording_format.width, recording_format.height, recording_format.frame_flags)


async def prompt(text: str) -> str:
    loop = asyncio.get_running_loop()
    return (await loop.run_in_executor(None, input, text)).strip()


async def confirm_sweep(
    candidates: list[int], fps: str, model_key: str, *, restore_enum: int | None
) -> bool:
    print(f"\nSweep plan — {len(candidates)} candidate dimension_enum value(s) will be SENT")
    print(f"to {model_key} at fps={fps}, one connected session, one window each:")
    print("  " + ", ".join(f"0x{c:02X}" for c in candidates))
    if restore_enum is not None:
        print(f"After the sweep, dimension_enum=0x{restore_enum:02X} will be sent to restore.")
    print(
        "\nThese are UNVERIFIED candidate video_format writes. The camera WILL change "
        "\nresolution/codec repeatedly. Keep it in view and be ready to intervene."
    )
    answer = await prompt("Type 'yes' to proceed: ")
    return answer.strip().lower() == "yes"


async def run(args: argparse.Namespace) -> int:
    profile = CameraProfile.for_model(model_key=args.model_key, firmware=args.firmware)
    spec = profile.require_command("video_format")
    fps = profile.require_fps_mode(args.fps)

    target_width = target_height = None
    if args.target_resolution:
        resolution = profile.require_resolution(args.target_resolution)
        target_width, target_height = resolution.width, resolution.height
    target_codec_id = None
    if args.target_codec:
        target_codec_id = profile.require_codec(args.target_codec).id

    candidates = compute_candidates(args, profile)
    if not candidates:
        print("No candidates left to sweep (everything in range is already known).")
        print("Pass --include-known to resweep, or widen --range/--enums.")
        return 1

    if not await confirm_sweep(
        candidates, args.fps, args.model_key, restore_enum=args.restore_enum
    ):
        print("Aborted before any write.")
        return 1

    decoder = ResultDecoder(profile)
    discovered = await scan_for_camera(profile.ble_name, timeout=args.timeout)
    cam = BMDCameraController(discovered=discovered, profile=profile)

    combined = CaptureSession()
    results: list[tuple[int, RecordingFormat | None, tuple | None, bool]] = []
    matched: list[int] = []
    stale_matches: list[int] = []
    # Carries forward across silent candidates: the most recent decoded
    # (width, height, flags), so a match can be checked against what the
    # camera was already reporting BEFORE this specific candidate was sent.
    last_state: tuple[int, int, int] | None = None

    await cam.connect()
    try:
        print(f"Waiting {args.connect_settle_seconds}s for the initial payload burst to settle…")
        await asyncio.sleep(args.connect_settle_seconds)

        for index, candidate in enumerate(candidates, start=1):
            command = encode_video_format(
                category=spec.category,
                parameter=spec.parameter,
                data_type=spec.data_type,
                fps_int=fps.fps_int,
                m_rate=fps.m_rate,
                dimension_enum=candidate,
                reserved=spec.reserved,
            )
            label = f"video_format probe enum=0x{candidate:02X} {args.fps}"
            print(f"\n--- [{index}/{len(candidates)}] Sending {label}")
            session = await run_send_and_capture(
                cam, [(label, command)], listen_seconds=args.listen_seconds
            )
            window = session.windows[-1]
            combined.windows.append(window)

            recording_format, codec_quality = decoder.decode(window)
            matched_this = is_match(
                recording_format,
                codec_quality,
                target_width=target_width,
                target_height=target_height,
                target_codec_id=target_codec_id,
            )
            results.append((candidate, recording_format, codec_quality, matched_this))
            print(f"  {describe_result(recording_format, codec_quality)}")

            current_state = recording_format_state(recording_format)

            if matched_this:
                matched.append(candidate)
                print(f"  ★★★ MATCH: enum=0x{candidate:02X} reached the target ★★★")
                if current_state is not None and current_state == last_state:
                    stale_matches.append(candidate)
                    print(
                        "  ⚠ POSSIBLE STALE MATCH: this report is byte-identical to what the "
                        "camera was already reporting BEFORE this candidate was sent — it may "
                        "be leftover state, not a result this candidate caused (the report "
                        "isn't an ack, it's a state reflection — docs/ble/settings.md §7). Do not "
                        "trust this as a confirmed dimension_enum without an independent "
                        "repeat from a genuinely different starting state."
                    )
                if args.stop_on_match:
                    answer = await prompt("Stop the sweep here? [Y/n]: ")
                    if answer.strip().lower() != "n":
                        break

            if current_state is not None:
                last_state = current_state

            if index < len(candidates):
                await asyncio.sleep(args.pause_seconds)

        if args.restore_enum is not None:
            print(f"\nRestoring with dimension_enum=0x{args.restore_enum:02X}…")
            restore_command = encode_video_format(
                category=spec.category,
                parameter=spec.parameter,
                data_type=spec.data_type,
                fps_int=fps.fps_int,
                m_rate=fps.m_rate,
                dimension_enum=args.restore_enum,
                reserved=spec.reserved,
            )
            restore_session = await run_send_and_capture(
                cam,
                [(f"restore enum=0x{args.restore_enum:02X}", restore_command)],
                listen_seconds=args.listen_seconds,
            )
            combined.windows.extend(restore_session.windows)
        else:
            print(
                "\nNo --restore-enum given — restore the camera to a safe state on the "
                "body if the sweep left it somewhere unexpected."
            )
    finally:
        await cam.disconnect()

    saved_path = save_capture(args.model_key, args.firmware, combined)
    print(f"\nCapture saved to: {saved_path}")

    print("\n=== Sweep summary ===")
    for candidate, recording_format, codec_quality, matched_this in results:
        if matched_this:
            marker = (
                " <-- STALE MATCH (see warning above)"
                if candidate in stale_matches
                else " <-- MATCH"
            )
        else:
            marker = ""
        print(f"  0x{candidate:02X}: {describe_result(recording_format, codec_quality)}{marker}")

    if matched:
        clean = [c for c in matched if c not in stale_matches]
        if clean:
            enums = ", ".join(f"0x{c:02X}" for c in clean)
            print(
                f"\n{len(clean)} candidate(s) matched: {enums}. Add the confirmed enum to the "
                "profile's resolutions table (dimension_enums) and re-run the family's normal "
                "write+echo confirmation (docs/ble/settings.md's runbook) before trusting it."
            )
        if stale_matches:
            enums = ", ".join(f"0x{c:02X}" for c in stale_matches)
            print(
                f"\n{len(stale_matches)} candidate(s) matched but look STALE: {enums}. Their "
                "reported state was identical to whatever the camera already held before that "
                "candidate was sent, so the report may not be a genuine result of this write. "
                "Re-run from a different starting state (or interleaved) before trusting these."
            )
        return 0 if clean else 1

    print(
        "\nNo candidate matched the target. If every candidate in range is now tried, "
        "the gap may need a different approach (see docs/ble/settings.md for open hypotheses) "
        "rather than a wider sweep."
    )
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Exhaustive video_format dimension_enum sweep: sends every candidate in one "
            "connected session and decodes the result from the wire (recording_format / "
            "codec_quality reports) instead of relying on the on-screen display, which is "
            "known-unreliable on at least one camera (see docs/ble/settings.md §15). No "
            "defaults for --model-key/--firmware — be explicit about which camera you are "
            "changing."
        )
    )
    parser.add_argument("--model-key", required=True, help="Camera model key, e.g. POCKET_6K_PRO")
    parser.add_argument("--firmware", required=True, help="Camera firmware, e.g. v8.6")
    parser.add_argument(
        "--fps", required=True, help="FPS label from the profile's fps_modes table, e.g. 25"
    )
    parser.add_argument(
        "--enums",
        help=(
            "Comma-separated dimension_enum candidates to sweep (accepts 0x.. hex or "
            "decimal), e.g. 0x00,0x01,0x04,0x05. Overrides --range/the default range."
        ),
    )
    parser.add_argument(
        "--range",
        nargs=2,
        type=lambda s: int(s, 0),
        metavar=("START", "END"),
        help=(
            f"Inclusive dimension_enum range to sweep (accepts 0x.. hex or decimal). "
            f"Default: 0x{DEFAULT_RANGE[0]:02X}-0x{DEFAULT_RANGE[1]:02X}, matching the "
            f"range the G2's own exhaustive 4K DCI/ProRes search covered "
            f"(docs/ble/settings.md §7-§8). Ignored if --enums is given."
        ),
    )
    parser.add_argument(
        "--include-known",
        action="store_true",
        help=(
            "Also sweep dimension_enum values already recorded in the profile's "
            "resolutions table. Default: excluded, since those are already answered."
        ),
    )
    parser.add_argument(
        "--target-resolution",
        help=(
            'Resolution label from the profile, e.g. "4K DCI" — a candidate whose '
            "resulting recording_format report matches this resolution's width/height "
            "is flagged as a MATCH."
        ),
    )
    parser.add_argument(
        "--target-codec",
        help=(
            "Codec name from the profile's codecs table, e.g. ProRes — combined with "
            "--target-resolution, a MATCH also requires the resulting codec_quality "
            "report's codec_id to match. Optional even when --target-resolution is set: "
            "video_format's write doesn't always trigger a fresh codec_quality report."
        ),
    )
    parser.add_argument(
        "--stop-on-match",
        action="store_true",
        default=True,
        help="Ask whether to stop the sweep as soon as a match is found. Default: on.",
    )
    parser.add_argument(
        "--no-stop-on-match",
        dest="stop_on_match",
        action="store_false",
        help="Keep sweeping every candidate even after a match is found.",
    )
    parser.add_argument(
        "--restore-enum",
        type=lambda s: int(s, 0),
        default=None,
        help=(
            "Optional dimension_enum to send after the sweep (or after an early stop) to "
            "restore the camera to a known state, e.g. the resolution/codec it started in."
        ),
    )
    parser.add_argument(
        "--listen-seconds",
        type=float,
        default=3.0,
        help="Seconds to listen for a response after each candidate. Default: 3.0",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=1.5,
        help="Seconds to pause between candidates, after one window closes. Default: 1.5",
    )
    parser.add_argument(
        "--connect-settle-seconds",
        type=float,
        default=6.0,
        help=(
            "Seconds to wait after connecting, before the first send, for the camera's "
            "post-connect initial-payload burst to drain (matches CameraSession's "
            "connect_settle_s default). Default: 6.0"
        ),
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0, help="BLE scan timeout in seconds. Default: 15.0"
    )
    return parser.parse_args()


if __name__ == "__main__":
    configure_console_logging()
    raise SystemExit(asyncio.run(run(parse_args())))
