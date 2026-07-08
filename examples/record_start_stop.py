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

Usage:
    python examples/record_start_stop.py

Edit MODEL_KEY / FIRMWARE / CYCLES / RECORD_SECONDS below to target a
different camera, change how many start/stop cycles to run, or how long
each recording lasts.
"""

import asyncio
import logging

from bmd_ble import BMDVerificationError, CameraSession

MODEL_KEY = "URSA_MINI_PRO_12K"
FIRMWARE = "v8.1"
# MODEL_KEY = "POCKET_6K_PRO"
# FIRMWARE = "v8.6"

CYCLES = 3
RECORD_SECONDS = 5
PAUSE_BETWEEN_CYCLES_S = 2


async def _confirm(action, label: str) -> bool:
    """Run a CameraSession action, returning whether its echo confirmed it."""
    try:
        await action()
    except BMDVerificationError as exc:
        print(f"{label} NOT confirmed: {exc}")
        return False
    print(f"{label} confirmed by echo ✓")
    return True


def _print_summary(results: list[tuple[int, bool, bool]]) -> None:
    total = len(results)
    start_confirmed = sum(1 for _, start_ok, _ in results if start_ok)
    stop_confirmed = sum(1 for _, _, stop_ok in results if stop_ok)

    print("\n=== Summary ===")
    print(f"record_start confirmed: {start_confirmed}/{total}")
    print(f"record_stop confirmed:  {stop_confirmed}/{total}")
    for cycle, start_ok, stop_ok in results:
        print(
            f"  Cycle {cycle}: start={'OK' if start_ok else 'FAILED'} "
            f"stop={'OK' if stop_ok else 'FAILED'}"
        )


async def main() -> None:
    results: list[tuple[int, bool, bool]] = []

    async with CameraSession(MODEL_KEY, FIRMWARE) as session:
        for cycle in range(1, CYCLES + 1):
            print(f"\n=== Cycle {cycle}/{CYCLES} ===")

            start_ok = await _confirm(session.record_start, "record_start")
            if start_ok:
                await asyncio.sleep(RECORD_SECONDS)

            stop_ok = await _confirm(session.record_stop, "record_stop")
            results.append((cycle, start_ok, stop_ok))

            if cycle < CYCLES:
                await asyncio.sleep(PAUSE_BETWEEN_CYCLES_S)

    _print_summary(results)

    if not all(start_ok and stop_ok for _, start_ok, stop_ok in results):
        raise SystemExit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
