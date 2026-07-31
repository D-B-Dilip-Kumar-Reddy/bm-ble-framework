"""
Start recording, wait, then stop recording — repeated across several cycles
to confirm CameraSession's echo-based verification is consistent, not just a
one-off (CLAUDE.md design principle 3).

record_start()/record_stop() raise BMDVerificationError if the camera's
INCOMING_CONTROL echo doesn't arrive or doesn't confirm the expected state
within the session's echo timeout — this script never assumes success from
"the write didn't raise." Every cycle runs regardless of earlier failures,
and a summary at the end reports how many start/stop echoes were confirmed
out of the total, plus a per-cycle breakdown.

Each cycle also captures the camera's TIMECODE reading at the moment
record_start and record_stop are each confirmed, and prints the resulting
clip duration via CameraSession.last_clip_duration_seconds() — see
docs/timecode.md for why this is hours/minutes/seconds precision only today
(the TIMECODE value's 4th field isn't decoded into the duration yet, pending
real-hardware confirmation of what it means).

Instead of a blind `asyncio.sleep(RECORD_SECONDS)`, the recording-hold step
uses `CameraSession.wait_while_recording()`, which returns early if the
camera stops recording on its own — observed on real hardware when the SD
card's write speed can't keep up (see docs/recording.md). A cycle that stops
early is reported as such rather than silently waiting out the rest of the
planned duration.

Usage:
    python examples/record_start_stop.py

Edit MODEL_KEY / FIRMWARE / CYCLES / RECORD_SECONDS below to target a
different camera, change how many start/stop cycles to run, or how long
each recording lasts.
"""

import asyncio
import logging
import sys

from bmd_ble import BMDVerificationError, CameraSession

MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v8.6"
# MODEL_KEY = "POCKET_6K_PRO"
# FIRMWARE = "v8.6"

CYCLES = 3
RECORD_SECONDS = 5
PAUSE_BETWEEN_CYCLES_S = 2


async def _confirm(action, label: str, *, success_msg: str | None = None) -> bool:
    """Run a CameraSession action, returning whether it succeeded.

    `success_msg` overrides the default "confirmed by echo" wording — used
    when record_stop() no-ops because the camera already stopped on its own
    (see CameraSession.record_stop's docstring), which isn't an echo at all.
    """
    try:
        await action()
    except BMDVerificationError as exc:
        print(f"{label} NOT confirmed: {exc}")
        return False
    print(success_msg or f"{label} confirmed by echo ✓")
    return True


def _format_timecode(tc) -> str:
    if tc is None:
        return "n/a"
    return f"{tc.hours:02d}:{tc.minutes:02d}:{tc.seconds:02d}:{tc.frames:02d}"


def _print_summary(results: list[tuple[int, bool, bool, bool, float | None]]) -> None:
    total = len(results)
    start_confirmed = sum(1 for _, start_ok, _, _, _ in results if start_ok)
    stop_confirmed = sum(1 for _, _, stop_ok, _, _ in results if stop_ok)
    stopped_early = sum(1 for _, _, _, early, _ in results if early)

    print("\n=== Summary ===")
    print(f"record_start confirmed: {start_confirmed}/{total}")
    print(f"record_stop confirmed:  {stop_confirmed}/{total}")
    print(f"stopped early:          {stopped_early}/{total}")
    for cycle, start_ok, stop_ok, early, duration in results:
        duration_str = f"{duration:.0f}s" if duration is not None else "n/a"
        early_str = " (stopped early)" if early else ""
        print(
            f"  Cycle {cycle}: start={'OK' if start_ok else 'FAILED'} "
            f"stop={'OK' if stop_ok else 'FAILED'} clip_duration={duration_str}{early_str}"
        )


async def main() -> None:
    results: list[tuple[int, bool, bool, bool, float | None]] = []

    async with CameraSession(MODEL_KEY, FIRMWARE) as session:
        for cycle in range(1, CYCLES + 1):
            print(f"\n=== Cycle {cycle}/{CYCLES} ===")

            start_ok = await _confirm(session.record_start, "record_start")
            stopped_early = False
            if start_ok:
                print(f"  start timecode: {_format_timecode(session.last_start_timecode)}")
                held = await session.wait_while_recording(RECORD_SECONDS)
                if not held:
                    stopped_early = True
                    print(
                        f"  recording stopped before the requested {RECORD_SECONDS}s "
                        f"(reason: {session.last_stop_reason}) — moving on"
                    )

            if stopped_early:
                stop_ok = await _confirm(
                    session.record_stop,
                    "record_stop",
                    success_msg="record_stop skipped — camera already stopped",
                )
            else:
                stop_ok = await _confirm(session.record_stop, "record_stop")
            if stop_ok:
                print(f"  stop timecode:  {_format_timecode(session.last_stop_timecode)}")

            duration = session.last_clip_duration_seconds() if start_ok and stop_ok else None
            if start_ok and stop_ok:
                duration_str = f"{duration:.0f}s" if duration is not None else "unavailable"
                print(f"  clip duration:  {duration_str}")

            results.append((cycle, start_ok, stop_ok, stopped_early, duration))

            if cycle < CYCLES:
                await asyncio.sleep(PAUSE_BETWEEN_CYCLES_S)

    _print_summary(results)

    if not all(start_ok and stop_ok for _, start_ok, stop_ok, _, _ in results):
        raise SystemExit(1)


if __name__ == "__main__":
    # Redirected/piped stdout is fully buffered by default in CPython, while
    # logging's StreamHandler flushes per record — without this, captured
    # log files interleave print() and logging output out of chronological
    # order, making TX/RX timing hard to trust when diagnosing issues later.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
