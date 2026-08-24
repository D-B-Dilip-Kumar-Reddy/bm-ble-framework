"""
tools/control/send_datetime_command.py
========================================
Actively sends a Category 7 ("Configuration") write to a real camera —
Timezone (7.2), Real Time Clock (7.0), or System language (7.1) — and
captures the response. See docs/ble/datetime.md.

WHY THIS TOOL EXISTS
-----------------------
Three passive real-hardware runs of tools/sniffers/sniffer_datetime.py
(docs/ble/datetime.md §4-§6) all found zero Category 7 activity on
INCOMING_CONTROL: neither genuine committed date/time/timezone changes on
the camera body, nor a full connect-time state burst (48 notifications
across 7 categories, including every Category 0x0C Lens parameter), ever
showed a single 0x07 report. Every passive avenue is exhausted. The one
thing not yet tried is a controller-initiated WRITE — this tool sends one
and asks the operator to watch the camera's own SETUP screen for whether it
actually changed, the same "operator's eyes are ground truth" stance
send_settings_command.py already uses for CANDIDATE writes with no reliable
echo.

THIS IS A GUESS, NOT A CONFIRMED ENCODING
--------------------------------------------
Unlike every other active-write tool in this codebase, this one has ZERO
real capture evidence behind its payload encoding — design principle 6 is
being knowingly stretched here, for a discovery probe, not a production
write. Nothing this tool sends should ever be copied into a profile's
`commands` block without independent real-hardware confirmation.

- `--parameter timezone` (7.2): plain `int32`, minutes offset from UTC per
  [spec] — no BCD, the least ambiguous of the three, and the easiest to
  visually confirm (the SETUP screen's own "TIME ZONE" field, and the
  displayed local time, should both shift). Recommended first target.
- `--parameter rtc` (7.0): `int32 x2`, [0] time, [1] date, both BCD per
  [spec]. Date is packed as `YYYYMMDD` (e.g. 2026-08-24 -> `0x20260824`) —
  reasonably unambiguous. Time's exact BCD shape is NOT specified by the
  spec beyond "BCD" — this tool's default hypothesis is `HHMMSS` packed
  with two trailing zero digits (`HHMMSS00`), by analogy with this
  codebase's own confirmed TIMECODE encoding (`docs/ble/timecode.md`,
  `HH:MM:SS:FF` BCD) with the frame-count digits zeroed instead of a frame
  count. `--raw-elements TIME DATE` bypasses this guess entirely for trying
  an alternative encoding.
- `--parameter language` (7.1): a 2-char ISO-639-1 string per [spec] — no
  BCD ambiguity, but this codebase has no existing string-payload encoder
  (`protocol/types.py`'s `DATA_TYPE_STRUCT_FORMATS` covers only the fixed-
  width numeric types) — sending this needs its own payload construction,
  built once this parameter's coordinate is confirmed to matter; not
  implemented in the first version of this tool. Use `timezone` or `rtc`
  first.

CONNECT SETTLE: the connect-burst run above showed the state dump can run
~8.6s. This tool waits `--connect-settle-seconds` (default 12.0, a margin
over the observed duration) before sending, so the write and its capture
window aren't buried in that burst — the same hazard send_settings_command.py
already documented and fixed for its own family.

FIRST RUN RESULT AND THE TWO NEW DISCOVERY AXES (2026-08-24, see
docs/ble/datetime.md §8): `--parameter timezone --minutes 345` sent a
correctly-formed `ASSIGN` (verified against the [spec] table byte-for-byte)
and the camera's SETUP screen did not visibly change at all — no BLE
traffic on category `0x07` either, same as every passive run before it.
Two untested wire coordinates could explain this, mirroring
send_settings_command.py's own discovery axes for its CANDIDATE families:

- `--reserved BYTE`: overrides header byte 3 (default `0x00`). This exact
  camera has a real precedent (`docs/ble/recording.md`) where the recording
  family silently required a specific reserved byte no camera-originated
  report ever revealed — a report need not carry the value a write requires.
- `--operation NAME`: overrides header byte 7 (default `ASSIGN`). A plain
  minutes-offset parameter is arguably a better semantic fit for `OFFSET`'s
  documented "add to current value" meaning than for an absolute `ASSIGN`
  target — `docs/ble/protocol.md` §4. **When using `--operation OFFSET`,
  `--minutes`/`--raw-elements` should be the delta you want to test, not an
  absolute target** — e.g. to nudge `UTC+05:30` (330) to `UTC+05:45` (345)
  via `OFFSET`, pass `--minutes 15`, not `345`. This tool does not compute
  the delta for you (it has no way to read the camera's current value), the
  same "operator supplies the delta" stance
  `send_settings_command.py --raw-payload --operation OFFSET` already uses.

Usage:
    # Safest first probe: timezone, an unambiguous plain int32
    python tools/control/send_datetime_command.py \\
        --model-key POCKET_6K_G2 --firmware v8.6 \\
        --parameter timezone --minutes 330

    # RTC (time+date), using this tool's default BCD hypothesis and the
    # current PC time/date
    python tools/control/send_datetime_command.py \\
        --model-key POCKET_6K_G2 --firmware v8.6 \\
        --parameter rtc

    # RTC with an explicit date/time instead of "now"
    python tools/control/send_datetime_command.py \\
        --model-key POCKET_6K_G2 --firmware v8.6 \\
        --parameter rtc --date 2026-08-24 --time 11:40:00

    # RTC with a raw element override, bypassing the BCD guess entirely
    python tools/control/send_datetime_command.py \\
        --model-key POCKET_6K_G2 --firmware v8.6 \\
        --parameter rtc --raw-elements 0x11400000 0x20260824

    # Discovery: try a different reserved byte after ASSIGN got no response
    python tools/control/send_datetime_command.py \\
        --model-key POCKET_6K_G2 --firmware v8.6 \\
        --parameter timezone --minutes 345 --reserved 0x01

    # Discovery: try OFFSET's delta semantics instead of an absolute ASSIGN
    # (a +15 minute delta from a starting UTC+05:30, not an absolute 345)
    python tools/control/send_datetime_command.py \\
        --model-key POCKET_6K_G2 --firmware v8.6 \\
        --parameter timezone --minutes 15 --operation OFFSET
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from capture import configure_console_logging, run_send_and_capture, save_capture  # noqa: E402

from bmd_camera.ble.camera_controller import BMDCameraController  # noqa: E402
from bmd_camera.ble.protocol.codec import (  # noqa: E402
    RESERVED_BYTE,
    Operation,
    encode_assign,
    encode_assign_elements,
)
from bmd_camera.ble.protocol.types import DataType  # noqa: E402
from bmd_camera.ble.scanner import scan_for_camera  # noqa: E402
from bmd_camera.camera_profile import CameraProfile  # noqa: E402

CATEGORY_CONFIGURATION = 0x07
PARAMETER_RTC = 0x00
PARAMETER_LANGUAGE = 0x01
PARAMETER_TIMEZONE = 0x02

PARAMETER_CHOICES = ("timezone", "rtc", "language")


def _pack_bcd_date(year: int, month: int, day: int) -> int:
    """[spec] date encoding: BCD YYYYMMDD, e.g. 2026-08-24 -> 0x20260824.
    Never sniffer-confirmed — see module docstring."""
    return int(f"{year:04d}{month:02d}{day:02d}", 16)


def _pack_bcd_time(hour: int, minute: int, second: int) -> int:
    """This tool's own hypothesis for the [spec]'s bare "BCD" time encoding:
    HHMMSS packed with two trailing zero digits, by analogy with this
    codebase's confirmed TIMECODE BCD shape (HH:MM:SS:FF) with the frame
    digits zeroed. NOT sniffer-confirmed — see module docstring."""
    return int(f"{hour:02d}{minute:02d}{second:02d}00", 16)


def _require_flags(args: argparse.Namespace, needed: tuple[str, ...]) -> None:
    missing = [flag for flag in needed if getattr(args, flag) is None]
    if missing:
        flags = ", ".join(f"--{flag.replace('_', '-')}" for flag in missing)
        raise SystemExit(f"--parameter {args.parameter} requires {flags}")


def resolve_reserved(args: argparse.Namespace) -> int:
    """The header reserved byte to encode with: `--reserved` if given, else
    the packet-header default (`0x00`) — see module docstring's FIRST RUN
    RESULT section. This category has no profile block to read a default
    from (nothing about it is confirmed yet), unlike
    `send_settings_command.py`'s `resolve_reserved`, which falls back to a
    CANDIDATE profile value."""
    if args.reserved is not None:
        return args.reserved
    return RESERVED_BYTE


def _reserved_override_suffix(resolved: int, args: argparse.Namespace) -> str:
    """Label suffix noting a `--reserved` override — empty string when
    unused, so the label matches this tool's pre-flag behavior exactly."""
    if args.reserved is None:
        return ""
    return f" reserved=0x{resolved:02X}(override; default 0x{RESERVED_BYTE:02X})"


def resolve_operation(args: argparse.Namespace) -> Operation:
    """The operation byte to encode with: `--operation` if given, else
    `Operation.ASSIGN` — every write this codebase has ever sent, across
    every family, until this flag existed."""
    if args.operation is not None:
        return Operation[args.operation]
    return Operation.ASSIGN


def _operation_override_suffix(resolved: Operation, args: argparse.Namespace) -> str:
    """Label suffix noting an `--operation` override — empty string when
    unused, matching `_reserved_override_suffix`'s evidence-visibility role."""
    if args.operation is None:
        return ""
    return f" operation={resolved.name}(0x{int(resolved):02X} override; default ASSIGN/0x00)"


def build_command(args: argparse.Namespace) -> tuple[str, bytes]:
    """Build (label, command_bytes) for the requested Category 7 parameter.
    Every value here is a candidate/guess (see module docstring) — never a
    confirmed protocol value."""
    resolved_reserved = resolve_reserved(args)
    resolved_operation = resolve_operation(args)
    suffix = _reserved_override_suffix(resolved_reserved, args) + _operation_override_suffix(
        resolved_operation, args
    )

    if args.parameter == "timezone":
        _require_flags(args, ("minutes",))
        label = f"datetime timezone minutes={args.minutes}" + suffix
        return label, encode_assign(
            category=CATEGORY_CONFIGURATION,
            parameter=PARAMETER_TIMEZONE,
            data_type=DataType.INT32,
            value=args.minutes,
            reserved=resolved_reserved,
            operation=resolved_operation,
        )

    if args.parameter == "rtc":
        if args.raw_elements is not None:
            time_value, date_value = args.raw_elements
            label = f"datetime rtc raw_elements=(0x{time_value:08X}, 0x{date_value:08X})" + suffix
        else:
            when = args.when
            time_value = _pack_bcd_time(when.hour, when.minute, when.second)
            date_value = _pack_bcd_date(when.year, when.month, when.day)
            label = (
                f"datetime rtc {when.isoformat(timespec='seconds')} "
                f"(time=0x{time_value:08X} date=0x{date_value:08X}, BCD hypothesis)" + suffix
            )
        return label, encode_assign_elements(
            category=CATEGORY_CONFIGURATION,
            parameter=PARAMETER_RTC,
            data_type=DataType.INT32,
            values=[time_value, date_value],
            reserved=resolved_reserved,
            operation=resolved_operation,
        )

    raise SystemExit(
        "--parameter language is not implemented yet — this codebase has no string-payload "
        "encoder (see module docstring). Try --parameter timezone or --parameter rtc first."
    )


async def confirm_send(label: str, command: bytes, model_key: str) -> bool:
    """Typed-yes gate before the write — this is a discovery-grade guess with
    zero real capture evidence behind it, not a CANDIDATE-but-transcribed
    value like send_settings_command.py's families."""
    tx = " ".join(f"{b:02X}" for b in command)
    print(f"\nAbout to send to {model_key}:")
    print(f"  {label}  ->  TX: {tx}")
    print(
        "\nThis payload encoding has NEVER been confirmed by a real capture (see this "
        "\ntool's module docstring) — it is a hypothesis built from the [spec] alone. "
        "\nIt WILL attempt to change the camera's date/time/timezone. Note the camera's "
        "\ncurrent SETUP > Date/Time screen before sending, keep it in view, and watch "
        "\nwhat it actually shows afterward — that is the only ground truth here, "
        "\nregardless of whether any BLE echo appears."
    )
    loop = asyncio.get_running_loop()
    answer = await loop.run_in_executor(None, input, "Type 'yes' to proceed: ")
    return answer.strip().lower() == "yes"


async def run(args: argparse.Namespace) -> int:
    label, command = build_command(args)

    if not await confirm_send(label, command, args.model_key):
        print("Aborted before any write.")
        return 1

    profile = CameraProfile.for_model(model_key=args.model_key, firmware=args.firmware)
    discovered = await scan_for_camera(profile.ble_name, timeout=args.timeout)
    cam = BMDCameraController(discovered=discovered, profile=profile)

    await cam.connect()
    try:
        # See docs/ble/datetime.md §6: a real connect-time state burst runs
        # ~8.6s on this camera — wait it out before sending so the write and
        # its capture window aren't buried inside it.
        print(f"Waiting {args.connect_settle_seconds}s for the connect-time burst to settle…")
        await asyncio.sleep(args.connect_settle_seconds)

        session = await run_send_and_capture(
            cam, [(label, command)], listen_seconds=args.listen_seconds
        )
        saved_path = save_capture(args.model_key, args.firmware, session)
        print(f"\nCapture saved to: {saved_path}")
        print(
            "\nDid the camera's SETUP > Date/Time screen actually change to match what "
            "was sent? That — not any BLE echo — is what determines whether this write "
            "was accepted. If it changed: this is real evidence for the encoding used "
            "above, worth recording in docs/ble/datetime.md. If category=0x07 also "
            "never appears in the capture above (as in every prior run) but the camera "
            "DID visibly change, that would show this category is write-only with no "
            "BLE-observable echo at all — mirroring the photo-capture precedent "
            "(docs/ble/photo_capture.md)."
        )
    finally:
        await cam.disconnect()

    return 0


def _parse_when(args: argparse.Namespace) -> datetime:
    """The target date/time for --parameter rtc: --date/--time if given
    (any subset — the other defaults from the current moment), else the
    current PC time in full."""
    now = datetime.now()
    date_str = args.date or now.strftime("%Y-%m-%d")
    time_str = args.time or now.strftime("%H:%M:%S")
    return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Actively send a discovery-grade, NEVER sniffer-confirmed Category 7 write "
            "(timezone / rtc / language) to a real camera and capture the response. "
            "See docs/ble/datetime.md and this tool's own module docstring before use. "
            "No defaults for --model-key/--firmware — be explicit about which camera "
            "you are changing."
        )
    )
    parser.add_argument("--model-key", required=True, help="Camera model key, e.g. POCKET_6K_G2")
    parser.add_argument("--firmware", required=True, help="Camera firmware, e.g. v8.6")
    parser.add_argument(
        "--parameter",
        required=True,
        choices=PARAMETER_CHOICES,
        help="Which Category 7 parameter to write (see docs/ble/datetime.md).",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=None,
        help="timezone only: minutes offset from UTC, e.g. 330 for UTC+05:30.",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="rtc only: target date, YYYY-MM-DD. Default: today (PC clock).",
    )
    parser.add_argument(
        "--time",
        default=None,
        help="rtc only: target time, HH:MM:SS. Default: now (PC clock).",
    )
    parser.add_argument(
        "--raw-elements",
        nargs=2,
        type=lambda s: int(s, 0),
        default=None,
        metavar=("TIME", "DATE"),
        help=(
            "rtc only: bypass this tool's BCD-packing guess entirely and send these two "
            "literal int32 element values (accepts 0x.. hex or decimal) as (time, date) "
            "instead of --date/--time."
        ),
    )
    parser.add_argument(
        "--reserved",
        type=lambda s: int(s, 0),
        default=None,
        help=(
            "Override the header reserved byte (accepts 0x.. hex or decimal). Default: "
            "unset, uses 0x00. Discovery-grade: this exact camera has a real precedent "
            "of silently requiring a specific reserved byte no report ever revealed — "
            "see module docstring's FIRST RUN RESULT section."
        ),
    )
    parser.add_argument(
        "--operation",
        choices=[o.name for o in Operation],
        default=None,
        help=(
            "Override the wire operation byte (header byte 7) instead of Operation.ASSIGN. "
            "Discovery-grade: try OFFSET's documented 'add to current value' semantics — "
            "see module docstring's FIRST RUN RESULT section for why --minutes/"
            "--raw-elements should be a DELTA, not an absolute target, when using this. "
            "Default: unset, ASSIGN unchanged."
        ),
    )
    parser.add_argument(
        "--listen-seconds",
        type=float,
        default=10.0,
        help="Seconds to listen for a response after the command. Default: 10.0",
    )
    parser.add_argument(
        "--connect-settle-seconds",
        type=float,
        default=12.0,
        help=(
            "Seconds to wait after connecting, before sending, for the connect-time "
            "state burst to drain (docs/ble/datetime.md §6 observed ~8.6s). Default: 12.0"
        ),
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0, help="BLE scan timeout in seconds. Default: 15.0"
    )
    args = parser.parse_args()

    if args.parameter == "rtc" and args.raw_elements is None:
        args.when = _parse_when(args)

    return args


if __name__ == "__main__":
    configure_console_logging()
    raise SystemExit(asyncio.run(run(parse_args())))
