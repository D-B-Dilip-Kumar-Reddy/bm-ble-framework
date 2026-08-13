"""
tools/control/sweep_camera_format.py
=====================================
Exhaustive (codec, quality variant, resolution, fps) verification sweep —
runs `CameraSession.set_camera_format()`, the real production API, across
every combination a profile's `codecs`/`resolutions`/`fps_modes` tables
claim the camera supports, in one connected session, and reports which
combinations actually confirm.

WHY THIS EXISTS
---------------
`POCKET_6K_PRO v8.6`'s ProRes/4K DCI gap (docs/ble/settings.md §16) was found by
accident — nobody set out to test that specific combination, it surfaced
mid-investigation of something else, and closing it took a full day of
targeted hypothesis testing (dimension_enum sweep, data_type byte,
video_format trailing elements, Operation.OFFSET, the exact camera-reported
fps and codec variant — see docs/ble/settings.md §16 for the whole trail) before
being accepted as a genuine software capability gap
(`resolutions.<name>.known_unreachable`, docs/ble/payload_profiles.md). Nothing
in this codebase's existing tooling would have caught it *before* a caller
hit it in production, and nothing currently checks whether a similar gap
exists on any of the other ~479 combinations these two profiles claim to
support (8 resolutions x ~4-8 variants x 8 fps modes, per codec, per
camera — see docs/ble/active_camera_control.md for the exact count).

This tool closes that gap systematically: it enumerates every combination a
profile's lookup tables claim is supported, runs each one through
`set_camera_format()`, and reports which combinations confirm cleanly, which
raise `BMDUnsupportedError` (camera doesn't offer it, or it's already a
known software gap), which raise `ValueError` (profile data — usually a
missing `dimension_enum` — hasn't been captured yet), and which raise
`BMDVerificationError` (attempted, but never confirmed — a genuine
candidate for a new `known_unreachable` entry, exactly the shape of finding
that took a full day to characterize by hand for ProRes/4K DCI). The saved
report is evidence for a human to review, not something this tool writes
into a profile itself — CLAUDE.md design principle 6 (sniffer-first) and
`docs/ble/payload_profiles.md`'s own framing of `known_unreachable` both apply
here: an unconfirmed candidate needs the same kind of real-hardware
follow-up (repeat runs, on-screen confirmation, ruling out an unrelated
no-op) that closed the ProRes/4K DCI investigation, not a mechanical
promotion from one sweep run.

DELIBERATE PRECEDENT BREAK: `CameraSession` in `tools/control/`
-----------------------------------------------------------------
Every other `tools/control/` script builds and sends raw protocol packets
directly (`encode_*`/`BMDCameraController.write_outgoing_control`) rather
than importing `CameraSession` — the low-level control this project's other
discovery-grade tooling needs (arbitrary `dimension_enum` probes,
`--raw-payload`, `--operation` overrides) isn't available through the
high-level API, and `CameraSession` has so far been reserved for
`examples/`. This tool is the deliberate exception: its entire purpose is
verifying the real production API — including its no-op guards
(`last_known_codec_variant`/`last_known_recording_format`), its
`known_unreachable` precondition check, and its proxy-resolution logic for
codecs without a direct `dimension_enum` — end to end, exactly as a real
caller would exercise it. Reimplementing that orchestration at the raw
protocol level would test a parallel code path, not the one this tool
exists to verify.

USAGE
-----
    # Preview the combination count and exact list without connecting to
    # anything — always start here given how large the full sweep can be.
    python tools/control/sweep_camera_format.py \\
        --model-key POCKET_6K_PRO --firmware v8.6 --dry-run

    # Full sweep (every combination the profile claims to support, minus
    # known_unreachable ones — see --include-known-unreachable):
    python tools/control/sweep_camera_format.py \\
        --model-key POCKET_6K_PRO --firmware v8.6

    # Narrow to specific resolutions/codecs/variants/fps — the practical
    # way to run this given a full sweep can mean hundreds of writes:
    python tools/control/sweep_camera_format.py \\
        --model-key POCKET_6K_PRO --firmware v8.6 \\
        --resolutions "4K DCI,UHD" --codecs ProRes --fps 25,24

Each combination is a real settings write (echo-verified, same as every
other CANDIDATE settings send in this codebase) — typed-yes gated once for
the whole sweep, not per combination, given the count involved. Read the
printed plan (or run with `--dry-run` first) before confirming.

REAL-HARDWARE RESULTS (2026-07-24, `POCKET_6K_PRO v8.6`, first production run)
--------------------------------------------------------------------------
448 combinations, one connected session: 431 confirmed cleanly, 17
unconfirmed. A follow-up run with `--include-known-unreachable` (480
combinations) reproduced the identical 17, plus correctly classified all 32
ProRes/4K DCI combinations as `unsupported` via the `known_unreachable`
guard — no write attempted for any of them, exactly as designed. Full
write-up: `docs/ble/settings.md`.

The 17 unconfirmed split into two genuinely different findings once checked
against the real camera:

- **16 were a false alarm at the reporting layer, not the write layer**:
  every `BRAW <variant> 6K @ 59.94`/`@ 60` combination, for all 8 variants.
  The operator confirmed these fps values aren't offered by the camera's own
  UI at 6K at all — a real hardware ceiling, not a software write-path gap
  like ProRes/4K DCI. This is now modeled as `resolutions.6K.max_fps_int`
  (`docs/ble/payload_profiles.md`) and excluded from this tool's default sweep
  the same way `known_unreachable` combinations are (see
  `--include-unsupported-fps` to re-sweep them anyway).
- **1 was a genuine false negative in this tool's own default timeout**:
  `ProRes HQ HD @ 23.98` reported `unconfirmed` at 3.1s against the
  then-default 3.0s `--echo-timeout-seconds` — but the operator confirmed
  the write had actually succeeded on the camera. This is the same
  lens-metadata-burst confound documented elsewhere in this codebase
  (`docs/ble/session_and_verification.md`) delaying a genuine echo past a too-
  short timeout, this time demonstrated in this tool specifically rather
  than a single manual send. The default was raised from 3.0 to 6.0 as a
  direct result — a fast default here risks exactly this false-negative
  shape, and a full sweep's fast combinations (most complete in well under
  a second) are unaffected by a longer timeout; only genuinely slow ones pay
  for it.

Net effect: the very first production run of this tool caught a real,
previously-undocumented camera limitation (6K's fps ceiling) and a real bug
in the tool's own default timeout, on the very first try — exactly the
"catch it before a caller hits it in production" case this tool exists for.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from capture import CAPTURES_DIR, configure_console_logging  # noqa: E402

from bmd_camera import (  # noqa: E402
    BMDUnsupportedError,
    BMDVerificationError,
    CameraProfile,
    CameraSession,
)


def _split(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [part.strip() for part in raw.split(",") if part.strip() != ""]


def enumerate_combinations(
    profile: CameraProfile,
    *,
    resolutions: list[str] | None = None,
    codecs: list[str] | None = None,
    variants: list[str] | None = None,
    fps_modes: list[str] | None = None,
    include_known_unreachable: bool = False,
    include_unsupported_fps: bool = False,
) -> list[tuple[str, str, str, str]]:
    """Every (codec, variant, resolution, fps) combination the profile's
    lookup tables claim is supported, in profile-declaration order (matching
    JSON key order — deterministic, reproducible sweep runs), optionally
    narrowed by name filters (validated against the profile — an unknown
    name raises the same way `require_resolution`/`require_codec`/
    `require_fps_mode` do elsewhere in this codebase).

    A codec listed in a resolution's `known_unreachable` map is skipped by
    default (its outcome is already known — see docs/ble/settings.md §16 for
    the ProRes/4K DCI precedent) unless `include_known_unreachable` is set,
    e.g. to re-verify one after a suspected fix.

    An fps whose `fps_int` exceeds a resolution's `max_fps_int` (a real
    hardware ceiling, not a software gap — see `docs/ble/settings.md`'s 6K
    fps-ceiling finding) is likewise skipped by default unless
    `include_unsupported_fps` is set — re-sweeping a known camera limit
    would just reproduce the same `unsupported` result every time.
    """
    resolution_names = resolutions if resolutions is not None else list(profile.resolutions)
    for name in resolution_names:
        profile.require_resolution(name)
    codec_filter = set(codecs) if codecs is not None else None
    if codec_filter is not None:
        for name in codec_filter:
            profile.require_codec(name)
    variant_filter = set(variants) if variants is not None else None
    fps_names = fps_modes if fps_modes is not None else list(profile.fps_modes)
    for name in fps_names:
        profile.require_fps_mode(name)

    combos: list[tuple[str, str, str, str]] = []
    for resolution_name in resolution_names:
        resolution_spec = profile.require_resolution(resolution_name)
        allowed_fps_names = fps_names
        if not include_unsupported_fps and resolution_spec.max_fps_int is not None:
            allowed_fps_names = [
                fps_name
                for fps_name in fps_names
                if profile.require_fps_mode(fps_name).fps_int <= resolution_spec.max_fps_int
            ]
        for codec_name in resolution_spec.codecs:
            if codec_filter is not None and codec_name not in codec_filter:
                continue
            if not include_known_unreachable and codec_name in resolution_spec.known_unreachable:
                continue
            codec_spec = profile.require_codec(codec_name)
            for variant_name in codec_spec.variants:
                if variant_filter is not None and variant_name not in variant_filter:
                    continue
                for fps_name in allowed_fps_names:
                    combos.append((codec_name, variant_name, resolution_name, fps_name))
    return combos


@dataclass
class ComboResult:
    """One combination's outcome — `outcome` is one of:

    - "confirmed" — set_camera_format() completed, every step echo-verified
      (or correctly recognized as an already-satisfied no-op).
    - "unsupported" — BMDUnsupportedError: the camera doesn't offer this
      codec at this resolution, this fps exceeds the resolution's hardware
      `max_fps_int` ceiling (if swept with --include-unsupported-fps), or
      it's already a known software gap (`known_unreachable`, if swept with
      --include-known-unreachable).
    - "missing_data" — ValueError: profile data needed to attempt the write
      hasn't been captured yet (usually a missing `dimension_enum`) — not a
      confirmed failure, just an incomplete profile.
    - "unconfirmed" — BMDVerificationError: the write was attempted but
      never confirmed by echo. This is the outcome that matters most — a
      genuine candidate for a new `known_unreachable` entry, the same shape
      of finding the ProRes/4K DCI investigation eventually confirmed by
      hand (docs/ble/settings.md §16). Promoting one to `known_unreachable`
      still needs the same real-hardware follow-up that investigation took
      (repeat runs from a genuinely different starting state, on-screen
      confirmation, ruling out a redundant no-op) — this tool surfaces the
      candidate, it doesn't close the investigation.
    """

    codec: str
    variant: str
    resolution: str
    fps: str
    outcome: str
    detail: str | None
    elapsed_s: float

    @property
    def label(self) -> str:
        return f"{self.codec} {self.variant} {self.resolution} @ {self.fps}"


async def run_combo(session: CameraSession, codec: str, variant: str, resolution: str, fps: str):
    start = time.monotonic()
    try:
        await session.set_camera_format(codec, variant, resolution, fps)
    except BMDUnsupportedError as exc:
        outcome, detail = "unsupported", str(exc)
    except ValueError as exc:
        outcome, detail = "missing_data", str(exc)
    except BMDVerificationError as exc:
        outcome, detail = "unconfirmed", str(exc)
    else:
        outcome, detail = "confirmed", None
    return ComboResult(codec, variant, resolution, fps, outcome, detail, time.monotonic() - start)


def save_report(
    model_key: str,
    firmware: str,
    results: list[ComboResult],
    *,
    captures_dir: Path = CAPTURES_DIR,
) -> Path:
    out_dir = captures_dir / f"{model_key}_{firmware}"
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{model_key}_{firmware}_format_sweep_{timestamp}.json"
    payload = {
        "model_key": model_key,
        "firmware": firmware,
        "swept_at": datetime.now().isoformat(timespec="seconds"),
        "results": [asdict(result) for result in results],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


def print_plan(combos: list[tuple[str, str, str, str]]) -> None:
    print(f"\n{len(combos)} combination(s) to sweep:")
    for codec, variant, resolution, fps in combos:
        print(f"  {codec} {variant} {resolution} @ {fps}")


async def prompt(text: str) -> str:
    loop = asyncio.get_running_loop()
    return (await loop.run_in_executor(None, input, text)).strip()


async def confirm_sweep(
    combos: list[tuple[str, str, str, str]],
    model_key: str,
    *,
    pause_s: float,
    echo_timeout_s: float,
) -> bool:
    print_plan(combos)
    worst_case_per_combo = 3 * echo_timeout_s + pause_s
    estimated_minutes = (len(combos) * worst_case_per_combo) / 60
    print(
        f"\nThis is a real settings write per combination, echo-verified — WILL change "
        f"\n{model_key}'s codec/quality/resolution/fps repeatedly, up to {len(combos)} times. "
        f"\nWorst-case estimate: ~{estimated_minutes:.1f} minutes (each combo up to "
        f"{worst_case_per_combo:.1f}s). Keep the camera powered and in view."
    )
    answer = await prompt("Type 'yes' to proceed: ")
    return answer.strip().lower() == "yes"


def print_summary(results: list[ComboResult]) -> None:
    print("\n=== Sweep summary ===")
    counts: dict[str, int] = {}
    for result in results:
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
        marker = {
            "confirmed": "OK  ",
            "unsupported": "SKIP",
            "missing_data": "DATA",
            "unconfirmed": "FAIL",
        }.get(result.outcome, "????")
        print(f"  [{marker}] {result.label} ({result.elapsed_s:.1f}s)")

    print(
        f"\n{counts.get('confirmed', 0)} confirmed, {counts.get('unsupported', 0)} unsupported, "
        f"{counts.get('missing_data', 0)} missing profile data, "
        f"{counts.get('unconfirmed', 0)} unconfirmed"
    )

    unconfirmed = [r for r in results if r.outcome == "unconfirmed"]
    if unconfirmed:
        print(
            "\nUNCONFIRMED — candidates for a new resolutions.<name>.known_unreachable entry "
            "(see docs/ble/payload_profiles.md). Each still needs the same real-hardware "
            "follow-up docs/ble/settings.md §16's ProRes/4K DCI investigation took before being "
            "written into the profile — repeat from a genuinely different starting state, "
            "confirm on-screen, rule out a redundant no-op:"
        )
        for result in unconfirmed:
            print(f"  {result.label}: {result.detail}")


async def run(args: argparse.Namespace) -> int:
    profile = CameraProfile.for_model(model_key=args.model_key, firmware=args.firmware)
    combos = enumerate_combinations(
        profile,
        resolutions=_split(args.resolutions),
        codecs=_split(args.codecs),
        variants=_split(args.variants),
        fps_modes=_split(args.fps),
        include_known_unreachable=args.include_known_unreachable,
        include_unsupported_fps=args.include_unsupported_fps,
    )

    if not combos:
        print("No combinations to sweep — check --resolutions/--codecs/--variants/--fps filters.")
        return 1

    if args.dry_run:
        print_plan(combos)
        print("\n--dry-run: no connection made, nothing sent.")
        return 0

    if not await confirm_sweep(
        combos, args.model_key, pause_s=args.pause_seconds, echo_timeout_s=args.echo_timeout_seconds
    ):
        print("Aborted before any write.")
        return 1

    results: list[ComboResult] = []
    async with CameraSession(
        args.model_key,
        args.firmware,
        echo_timeout_s=args.echo_timeout_seconds,
        connect_settle_s=args.connect_settle_seconds,
    ) as session:
        for index, (codec, variant, resolution, fps) in enumerate(combos, start=1):
            print(f"\n--- [{index}/{len(combos)}] {codec} {variant} {resolution} @ {fps}")
            result = await run_combo(session, codec, variant, resolution, fps)
            results.append(result)
            print(f"  {result.outcome}" + (f": {result.detail}" if result.detail else ""))
            if index < len(combos):
                await asyncio.sleep(args.pause_seconds)

    saved_path = save_report(args.model_key, args.firmware, results)
    print(f"\nReport saved to: {saved_path}")
    print_summary(results)

    return 1 if any(r.outcome == "unconfirmed" for r in results) else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Exhaustive (codec, variant, resolution, fps) verification sweep via "
            "CameraSession.set_camera_format() — the real production API. WILL change "
            "camera settings repeatedly. No defaults for --model-key/--firmware — be "
            "explicit about which camera you are changing."
        )
    )
    parser.add_argument("--model-key", required=True, help="Camera model key, e.g. POCKET_6K_PRO")
    parser.add_argument("--firmware", required=True, help="Camera firmware, e.g. v8.6")
    parser.add_argument(
        "--resolutions",
        help="Comma-separated resolution labels to sweep, e.g. '4K DCI,UHD'. Default: all.",
    )
    parser.add_argument(
        "--codecs",
        help="Comma-separated codec names to sweep, e.g. ProRes,BRAW. Default: all.",
    )
    parser.add_argument(
        "--variants",
        help=(
            "Comma-separated quality variant names to sweep, e.g. HQ,422. Applied per codec "
            "(a codec with no matching variant is skipped entirely for it). Default: all."
        ),
    )
    parser.add_argument(
        "--fps",
        help="Comma-separated fps labels to sweep, e.g. 25,24,23.98. Default: all.",
    )
    parser.add_argument(
        "--include-known-unreachable",
        action="store_true",
        help=(
            "Also sweep (codec, resolution) pairs already recorded in the profile's "
            "known_unreachable map (see docs/ble/payload_profiles.md) — e.g. to re-verify one "
            "after a suspected fix. Default: excluded, since those outcomes are already known "
            "and would just raise BMDUnsupportedError immediately."
        ),
    )
    parser.add_argument(
        "--include-unsupported-fps",
        action="store_true",
        help=(
            "Also sweep fps values above a resolution's known max_fps_int hardware ceiling "
            "(e.g. POCKET_6K_PRO v8.6's 6K topping out at 50, see docs/ble/settings.md). "
            "Default: excluded, since those are a real camera limit, not a software gap, "
            "and would just raise BMDUnsupportedError immediately."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the combination count and plan, then exit — no connection, no writes.",
    )
    parser.add_argument(
        "--echo-timeout-seconds",
        type=float,
        default=6.0,
        help=(
            "Per-write echo timeout, passed to CameraSession. Default: 6.0 — deliberately "
            "above CameraSession's own 3.0s default; see the module docstring's REAL-HARDWARE "
            "RESULTS section for the false negative that motivated this."
        ),
    )
    parser.add_argument(
        "--connect-settle-seconds",
        type=float,
        default=6.0,
        help="Seconds to wait after connecting for the initial payload burst to drain. Default: 6",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=2.0,
        help="Seconds to pause between combinations, after one completes. Default: 2.0",
    )
    return parser.parse_args()


if __name__ == "__main__":
    configure_console_logging()
    raise SystemExit(asyncio.run(run(parse_args())))
