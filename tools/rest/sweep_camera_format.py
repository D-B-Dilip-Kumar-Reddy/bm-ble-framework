"""
tools/rest/sweep_camera_format.py
====================================
Exhaustive (codec, quality variant, resolution, fps, sensor area) verification
sweep — runs `RestCameraSession.set_camera_format()`, the real production
API, across every combination a profile's `codecs`/`resolutions`/`fps_modes`
tables claim, plus every distinct `sensorResolution` the camera's own live
`GET /system/supportedFormats` pairs with each one, in one connected
session, and reports which combinations actually confirm. The REST analogue
of `tools/control/sweep_camera_format.py`.

WHY THIS EXISTS
---------------
Two real defects surfaced from exactly two manual `set_camera_format()`
calls in a row (`examples/rest_change_format.py`'s first run,
`POCKET_6K_G2 v8.6`, 2026-08-03, see docs/rest/session.md): a `sensorResolution`
carried over stale across a codec switch, rejected by the camera with a real
`400`. Nothing in this codebase's existing REST tooling would have caught
that *before* a caller hit it, and nothing currently checks whether a
similar gap exists on any of the other combinations these profiles claim to
support. This tool closes that gap systematically, the same way
`tools/control/sweep_camera_format.py` already does for BLE.

**REST adds a dimension BLE never had: sensor area.** `GET
/system/supportedFormats` can pair the *same* `(codec, recordResolution, fps)`
with more than one `sensorResolution` — confirmed on real hardware for
`ProRes` at `1920x1080`, which pairs with three (`docs/rest/transport.md`).
`RestCameraSession.set_camera_format()` refuses an ambiguous combination by
default (`BMDUnsupportedError` — see its docstring) rather than guessing;
this tool is what actually exercises every one of those pairings, via the
`sensor_resolution` parameter `set_camera_format()` gained specifically for
this purpose. Sweeping "sensor area" is therefore not exploring some
separate, disconnected axis — it is testing every distinct write body the
camera's own capability matrix says is valid for a given
codec/resolution/fps, which `set_camera_format()`'s ordinary single-call
usage never reaches on its own.

WHAT THIS SWEEPS, AND HOW IT DIFFERS FROM THE BLE TOOL
--------------------------------------------------------
Combinations are enumerated the same way the BLE tool does — from the
profile's `codecs`/`resolutions`/`fps_modes` tables, in declaration order,
optionally narrowed by name filters. Unlike BLE, **no `known_unreachable`
or `max_fps_int` filtering is applied**: those are BLE-write-path-specific
concepts (design principle 7) that REST's live capability check makes
unnecessary — `docs/rest/session.md`'s `set_camera_format` section spells
this out explicitly ("None of BLE's `dimension_enums`/`m_rate`/
`frame_flags`/`known_unreachable`/`max_fps_int` are consulted here"). This
is deliberate and has real evidentiary value: `known_unreachable` entries
are exactly the combinations worth re-testing over REST, since REST already
reached one of them (`ProRes/4K DCI`, `docs/ble/settings.md` §16.2) that
BLE's write path cannot.

Each `(codec, variant, resolution, fps)` combination is then expanded,
using one live `GET /system/supportedFormats` read taken right after
connecting, into one sweep item per distinct `sensorResolution` the camera
pairs with it — or, if the camera doesn't offer the combination at all,
one `"unsupported"` item with no write attempted (the live matrix already
answers that question; sending a doomed write teaches nothing new, the same
reasoning `tools/control/sweep_camera_format.py` applies to
`known_unreachable`/`max_fps_int`).

`--dry-run` here still connects (read-only: it only calls
`supported_formats()`) rather than staying fully offline like the BLE
tool's — sensor-area expansion needs live data, so an offline count would
undercount every ambiguous combination. Nothing is written either way.

USAGE
-----
    # Preview the fully expanded plan (including sensor-area duplicates) —
    # read-only, connects but never writes:
    python tools/rest/sweep_camera_format.py \\
        --host pocket-cinema-camera-6k-g2.local \\
        --model-key POCKET_6K_G2 --firmware v8.6 --dry-run

    # Full sweep:
    python tools/rest/sweep_camera_format.py \\
        --host pocket-cinema-camera-6k-g2.local \\
        --model-key POCKET_6K_G2 --firmware v8.6

    # Narrow to specific resolutions/codecs/variants/fps — the practical way
    # to run this given a full sweep can mean hundreds of writes:
    python tools/rest/sweep_camera_format.py \\
        --host pocket-cinema-camera-6k-g2.local \\
        --model-key POCKET_6K_G2 --firmware v8.6 \\
        --resolutions "4K DCI,UHD" --codecs ProRes --fps 25,24

Each combination is a real settings write (dual-check verified, same as
`examples/rest_change_format.py`) — typed-yes gated once for the whole
sweep, not per combination, given the count involved. Read the printed plan
(or run with `--dry-run` first) before confirming.

No real-hardware run of this tool has been reported yet.
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

from bmd_camera import (
    BMDUnsupportedError,
    BMDVerificationError,
    CameraProfile,
    RestCameraSession,
)
from bmd_camera.rest.mapping import resolve_rest_codec_name

CAPTURES_DIR = Path(__file__).resolve().parents[1] / "captures"


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
) -> list[tuple[str, str, str, str]]:
    """Every `(codec, variant, resolution, fps)` combination the profile's
    lookup tables claim, in profile-declaration order (deterministic,
    reproducible sweep runs), optionally narrowed by name filters
    (validated against the profile — an unknown name raises the same way
    `require_resolution`/`require_codec`/`require_fps_mode` do elsewhere).

    Deliberately does **not** filter out `known_unreachable` or
    `max_fps_int`-exceeding combinations the way
    `tools/control/sweep_camera_format.py` does for BLE — see the module
    docstring for why those are BLE-write-path-specific concepts REST's
    live capability check makes unnecessary here.
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
        for codec_name in resolution_spec.codecs:
            if codec_filter is not None and codec_name not in codec_filter:
                continue
            codec_spec = profile.require_codec(codec_name)
            for variant_name in codec_spec.variants:
                if variant_filter is not None and variant_name not in variant_filter:
                    continue
                for fps_name in fps_names:
                    combos.append((codec_name, variant_name, resolution_name, fps_name))
    return combos


@dataclass(frozen=True)
class SweepItem:
    """One `(codec, variant, resolution, fps)` combination expanded against
    the camera's own live `supported_formats()` — one item per distinct
    `sensorResolution` it pairs with the combination, or a single
    `offered=False` item when the camera doesn't offer the combination at
    all (no write is attempted for that case — see `run_combo`)."""

    codec: str
    variant: str
    resolution: str
    fps: str
    sensor_resolution: tuple[int, int] | None
    offered: bool

    @property
    def label(self) -> str:
        sensor = (
            f" [sensor {self.sensor_resolution[0]}x{self.sensor_resolution[1]}]"
            if (self.sensor_resolution is not None)
            else ""
        )
        return f"{self.codec} {self.variant} {self.resolution} @ {self.fps}{sensor}"


async def expand_with_sensor_resolutions(
    session: RestCameraSession,
    profile: CameraProfile,
    combos: list[tuple[str, str, str, str]],
) -> list[SweepItem]:
    """One live `GET /system/supportedFormats` read, then expand every
    `(codec, variant, resolution, fps)` combo into one `SweepItem` per
    distinct `sensorResolution` the camera pairs with it (the "sensor area"
    dimension the module docstring describes), or one `offered=False` item
    when the camera doesn't offer the combination at all."""
    formats = await session.supported_formats()
    items: list[SweepItem] = []
    for codec, variant, resolution, fps in combos:
        rest_codec = resolve_rest_codec_name(profile.rest.format_names, codec, variant)
        resolution_spec = profile.require_resolution(resolution)
        record_resolution = (resolution_spec.width, resolution_spec.height)
        sensor_resolutions = sorted(
            {
                entry.sensor_resolution
                for entry in formats
                if entry.record_resolution == record_resolution
                and rest_codec in entry.codecs
                and fps in entry.frame_rates
            }
        )
        if not sensor_resolutions:
            items.append(SweepItem(codec, variant, resolution, fps, None, offered=False))
            continue
        for sensor_resolution in sensor_resolutions:
            items.append(
                SweepItem(codec, variant, resolution, fps, sensor_resolution, offered=True)
            )
    return items


@dataclass
class ComboResult:
    """One sweep item's outcome — `outcome` is one of:

    - "confirmed" — `set_camera_format()` completed, the dual-check
      confirmed it.
    - "unsupported" — the camera's live `supported_formats()` doesn't offer
      this combination at all (no write attempted), or `set_camera_format()`
      itself raised `BMDUnsupportedError` (a capability-check path it
      re-checks internally, or a profile capability gate — see its
      docstring).
    - "missing_data" — `ValueError`: profile data needed to attempt the
      write hasn't been captured yet. Structurally shouldn't happen here,
      since combos are generated from the same tables `set_camera_format()`
      validates against — kept for the same defensive reason
      `tools/control/sweep_camera_format.py` keeps it.
    - "unconfirmed" — `BMDVerificationError`: the write was attempted but
      never confirmed by the dual-check. The outcome that matters most — a
      genuine candidate for further investigation, the same shape of
      finding `docs/ble/settings.md` §16's ProRes/4K DCI took a full day to
      characterize by hand for BLE.
    """

    codec: str
    variant: str
    resolution: str
    fps: str
    sensor_resolution: tuple[int, int] | None
    outcome: str
    detail: str | None
    elapsed_s: float

    @property
    def label(self) -> str:
        sensor = (
            f" [sensor {self.sensor_resolution[0]}x{self.sensor_resolution[1]}]"
            if (self.sensor_resolution is not None)
            else ""
        )
        return f"{self.codec} {self.variant} {self.resolution} @ {self.fps}{sensor}"


async def run_combo(session: RestCameraSession, item: SweepItem) -> ComboResult:
    if not item.offered:
        return ComboResult(
            item.codec,
            item.variant,
            item.resolution,
            item.fps,
            None,
            "unsupported",
            "not offered by GET /system/supportedFormats for this "
            "(codec, recordResolution, fps) — no write attempted",
            0.0,
        )
    start = time.monotonic()
    try:
        await session.set_camera_format(
            item.codec,
            item.variant,
            item.resolution,
            item.fps,
            sensor_resolution=item.sensor_resolution,
        )
    except BMDUnsupportedError as exc:
        outcome, detail = "unsupported", str(exc)
    except ValueError as exc:
        outcome, detail = "missing_data", str(exc)
    except BMDVerificationError as exc:
        outcome, detail = "unconfirmed", str(exc)
    else:
        outcome, detail = "confirmed", None
    return ComboResult(
        item.codec,
        item.variant,
        item.resolution,
        item.fps,
        item.sensor_resolution,
        outcome,
        detail,
        time.monotonic() - start,
    )


def save_report(
    model_key: str,
    firmware: str,
    results: list[ComboResult],
    *,
    captures_dir: Path = CAPTURES_DIR,
) -> Path:
    out_dir = captures_dir / "rest" / f"{model_key}_{firmware}"
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{model_key}_{firmware}_rest_format_sweep_{timestamp}.json"
    payload = {
        "model_key": model_key,
        "firmware": firmware,
        "swept_at": datetime.now().isoformat(timespec="seconds"),
        "results": [asdict(result) for result in results],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


def print_plan(items: list[SweepItem]) -> None:
    print(f"\n{len(items)} sweep item(s):")
    for item in items:
        marker = "" if item.offered else "  (not offered — no write)"
        print(f"  {item.label}{marker}")


async def prompt(text: str) -> str:
    loop = asyncio.get_running_loop()
    return (await loop.run_in_executor(None, input, text)).strip()


async def confirm_sweep(
    items: list[SweepItem],
    model_key: str,
    *,
    pause_s: float,
    verify_timeout_s: float,
) -> bool:
    print_plan(items)
    to_write = [item for item in items if item.offered]
    worst_case_per_combo = 2 * verify_timeout_s + pause_s
    estimated_minutes = (len(to_write) * worst_case_per_combo) / 60
    print(
        f"\n{len(to_write)} of {len(items)} item(s) will attempt a real write, dual-check "
        f"verified — WILL change {model_key}'s codec/quality/resolution/fps/sensor-area "
        f"repeatedly, up to {len(to_write)} times. Worst-case estimate: ~{estimated_minutes:.1f} "
        f"minutes (each combo up to {worst_case_per_combo:.1f}s). Keep the camera powered and "
        "in view."
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
            "\nUNCONFIRMED — genuine candidates for further investigation (see "
            "docs/ble/settings.md §16 for the shape of follow-up a candidate like this took "
            "for BLE's ProRes/4K DCI gap — repeat from a different starting state, confirm "
            "on-screen, rule out a redundant no-op):"
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
    )

    if not combos:
        print("No combinations to sweep — check --resolutions/--codecs/--variants/--fps filters.")
        return 1

    async with RestCameraSession(
        args.host,
        args.model_key,
        args.firmware,
        scheme=args.scheme,
        port=args.port,
        timeout_s=args.timeout,
        ws_timeout_s=args.timeout,
        verify_timeout_s=args.verify_timeout_seconds,
    ) as session:
        items = await expand_with_sensor_resolutions(session, profile, combos)

        if args.dry_run:
            print_plan(items)
            print("\n--dry-run: read-only GET /system/supportedFormats only, nothing written.")
            return 0

        if not await confirm_sweep(
            items,
            args.model_key,
            pause_s=args.pause_seconds,
            verify_timeout_s=args.verify_timeout_seconds,
        ):
            print("Aborted before any write.")
            return 1

        results: list[ComboResult] = []
        for index, item in enumerate(items, start=1):
            print(f"\n--- [{index}/{len(items)}] {item.label}")
            result = await run_combo(session, item)
            results.append(result)
            print(f"  {result.outcome}" + (f": {result.detail}" if result.detail else ""))
            if index < len(items):
                await asyncio.sleep(args.pause_seconds)

    saved_path = save_report(args.model_key, args.firmware, results)
    print(f"\nReport saved to: {saved_path}")
    print_summary(results)

    return 1 if any(r.outcome == "unconfirmed" for r in results) else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Exhaustive (codec, variant, resolution, fps, sensor area) verification sweep "
            "via RestCameraSession.set_camera_format() — the real production API. WILL "
            "change camera settings repeatedly. No defaults for --host/--model-key/"
            "--firmware — be explicit about which camera you are changing."
        )
    )
    parser.add_argument(
        "--host",
        required=True,
        help="Camera hostname or IP, e.g. pocket-cinema-camera-6k-g2.local. See "
        "docs/rest/transport.md for how to find this over USB.",
    )
    parser.add_argument(
        "--scheme",
        default="http",
        choices=["http", "https"],
        help="URL scheme when --host has none. Default: http.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port override, if the camera doesn't use the scheme's default.",
    )
    parser.add_argument("--model-key", required=True, help="Camera model key, e.g. POCKET_6K_G2")
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
        "--dry-run",
        action="store_true",
        help="Connect and print the fully expanded plan (including sensor-area duplicates), "
        "then exit — read-only (GET /system/supportedFormats only), no writes.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-request / WS-connect timeout in seconds, passed to RestCameraSession as "
        "both timeout_s and ws_timeout_s. Default: 5.0",
    )
    parser.add_argument(
        "--verify-timeout-seconds",
        type=float,
        default=6.0,
        help=(
            "Per-write dual-check verification timeout, passed to RestCameraSession as "
            "verify_timeout_s. Default: 6.0 — deliberately above RestCameraSession's own "
            "5.0s default, mirroring the false-negative lesson "
            "tools/control/sweep_camera_format.py's module docstring records for BLE's "
            "echo timeout; no REST-side false negative has been observed yet, but a full "
            "sweep's fast combinations (most complete well under the timeout) are unaffected "
            "by a longer one, while a genuinely slow one only pays for it if it needs to."
        ),
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=2.0,
        help="Seconds to pause between combinations, after one completes. Default: 2.0",
    )
    return parser.parse_args()


if __name__ == "__main__":
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.stdout.reconfigure(line_buffering=True)
    raise SystemExit(asyncio.run(run(parse_args())))
