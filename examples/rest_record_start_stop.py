"""
Start recording, wait, then stop recording — entirely over REST, no BLE
involved. Repeated across several cycles to confirm RestCameraSession's
dual-check verification (WS event primary, GET readback secondary — design
principle 3's REST sibling) is consistent, not just a one-off.

record_start()/record_stop() raise BMDVerificationError if neither the WS
`propertyValueChanged` event nor a GET readback confirms the expected
recording state within the session's verify_timeout_s — this script never
assumes success from "the PUT returned 204." record_start() also raises
BMDStorageError first if no active storage device has remaining record time
(design principle 10) — this script reports that as a failed cycle rather
than crashing, so a full card is visible in the summary like any other
failure.

Instead of a blind `asyncio.sleep(RECORD_SECONDS)`, the recording-hold step
uses `RestCameraSession.wait_while_recording()`, which returns early if
`is_recording` becomes False before the timeout (e.g. a camera-initiated
stop reported over the WS event feed).

STATUS: real-hardware confirmation of record_start()/record_stop() over
REST is still pending — see docs/rest/session.md's "Write verbs" section.
`PUT /transports/0/record` was deliberately never probed by
tools/rest/probe_endpoints.py (it would start/stop a real recording), so
this script's first successful run against real hardware *is* that
confirmation.

Edit HOST / MODEL_KEY / FIRMWARE below to target a different camera, and
CYCLES / RECORD_SECONDS to change how many start/stop cycles to run or how
long each recording lasts.

Usage:
    python examples/rest_record_start_stop.py
"""

import asyncio
import logging
import sys

from bmd_camera import BMDVerificationError, RestCameraSession
from bmd_camera.exceptions import BMDStorageError

HOST = "pocket-cinema-camera-6k-pro.local"
MODEL_KEY = "POCKET_6K_PRO"
FIRMWARE = "v8.6"
# HOST = "pocket-cinema-camera-6k-g2.local"
# MODEL_KEY = "POCKET_6K_G2"
# FIRMWARE = "v8.6"

CYCLES = 3
RECORD_SECONDS = 5
PAUSE_BETWEEN_CYCLES_S = 2


async def _confirm(action, label: str) -> bool:
    """Run a RestCameraSession action, returning whether it succeeded."""
    try:
        await action()
    except (BMDVerificationError, BMDStorageError) as exc:
        print(f"{label} NOT confirmed: {exc}")
        return False
    print(f"{label} confirmed ✓")
    return True


def _print_summary(results: list[tuple[int, bool, bool, bool]]) -> None:
    total = len(results)
    start_confirmed = sum(1 for _, start_ok, _, _ in results if start_ok)
    stop_confirmed = sum(1 for _, _, stop_ok, _ in results if stop_ok)
    stopped_early = sum(1 for _, _, _, early in results if early)

    print("\n=== Summary ===")
    print(f"record_start confirmed: {start_confirmed}/{total}")
    print(f"record_stop confirmed:  {stop_confirmed}/{total}")
    print(f"stopped early:          {stopped_early}/{total}")
    for cycle, start_ok, stop_ok, early in results:
        early_str = " (stopped early)" if early else ""
        print(
            f"  Cycle {cycle}: start={'OK' if start_ok else 'FAILED'} "
            f"stop={'OK' if stop_ok else 'FAILED'}{early_str}"
        )


async def main() -> None:
    results: list[tuple[int, bool, bool, bool]] = []

    async with RestCameraSession(HOST, MODEL_KEY, FIRMWARE) as session:
        for cycle in range(1, CYCLES + 1):
            print(f"\n=== Cycle {cycle}/{CYCLES} ===")

            start_ok = await _confirm(session.record_start, "record_start")
            stopped_early = False
            if start_ok:
                held = await session.wait_while_recording(RECORD_SECONDS)
                if not held:
                    stopped_early = True
                    print(f"  recording stopped before the requested {RECORD_SECONDS}s")

            stop_ok = await _confirm(session.record_stop, "record_stop")

            results.append((cycle, start_ok, stop_ok, stopped_early))

            if cycle < CYCLES:
                await asyncio.sleep(PAUSE_BETWEEN_CYCLES_S)

    _print_summary(results)

    if not all(start_ok and stop_ok for _, start_ok, stop_ok, _ in results):
        raise SystemExit(1)


if __name__ == "__main__":
    # Redirected/piped stdout is fully buffered by default in CPython, while
    # logging's StreamHandler flushes per record — without this, captured
    # log files interleave print() and logging output out of chronological
    # order, making timing hard to trust when diagnosing issues later.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
