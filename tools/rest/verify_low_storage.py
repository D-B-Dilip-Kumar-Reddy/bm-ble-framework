"""
tools/rest/verify_low_storage.py
===================================
Real-hardware verification for `RestCameraSession.wait_for_low_storage()`
(Phase 8 item 1, `docs/rest/session.md`'s `last_known_storage`/
`wait_for_low_storage()` section) — the one piece of that feature that has
only run against the injected-fake unit test suite so far. The
`/media/workingset` WS push itself is already real-hardware-confirmed
(`tools/rest/watch_events.py`, `POCKET_6K_G2 v8.6`, 2026-08-05); this tool
is what exercises `wait_for_low_storage()`'s own threshold-crossing logic,
immediate-return shortcut, and return-value contract against a real
camera.

WHY A SMALLER CARD
-------------------
Every real-hardware run so far (`examples/rest_record_test_clip.py`, the
Phase 8 item 3 stress-test sweep) used a 1TB card, on which a meaningful
"low storage" threshold is impractical to reach in a normal session. A
128GB card makes crossing a real threshold reachable with an ordinary
test recording, without inventing a fake threshold so large it stops
meaning anything.

WHAT THIS TOOL DOES
--------------------
Connects, prints a baseline `storage_state()` snapshot (a plain `GET`, for
context — where the card actually stands right now), then calls
`wait_for_low_storage()` with the thresholds you pass and reports exactly
what it returned, how long it took, and the final `last_known_storage`
snapshot. It does not itself record anything — cross the threshold by
running a real recording concurrently, in another terminal, e.g.:

    python examples/rest_record_test_clip.py

(edit that script's CODEC/VARIANT/RESOLUTION/FPS/RECORD_SECONDS constants
for a combination and duration that will actually eat into a 128GB card
before this tool's own --timeout elapses), or just record on the camera
body directly.

Deliberately does not print live intermediate `/media/workingset` values
while waiting — `RestCameraSession` has no external hook for that without
adding one, and design principle 10 is explicit that storage state is
notification-driven, not polled. If you want to watch it change in real
time alongside this tool, run in a third terminal:

    python tools/rest/watch_events.py --host <host> --properties /media/workingset

USAGE
-----
    python tools/rest/verify_low_storage.py \\
        --host pocket-cinema-camera-6k-g2.local \\
        --model-key POCKET_6K_G2 --firmware v8.6 \\
        --min-space-bytes 10000000000 --timeout 1800

    # Or by remaining record time instead of space, or both (either alone
    # is enough — same requirement wait_for_low_storage() itself enforces):
    python tools/rest/verify_low_storage.py \\
        --host pocket-cinema-camera-6k-g2.local \\
        --model-key POCKET_6K_G2 --firmware v8.6 \\
        --min-record-time-s 300 --timeout 1800

Read-only: no write ever leaves this script. --timeout only bounds how
long this tool waits; it does not touch the camera's own recording state.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from bmd_camera import RestCameraSession

logger = logging.getLogger(__name__)


def _format_bytes(num_bytes: int) -> str:
    gb = num_bytes / 1_000_000_000
    return f"{num_bytes} bytes ({gb:.2f} GB)"


def _format_seconds(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{seconds}s ({hours:d}h{minutes:02d}m{secs:02d}s)"


def _print_storage(label: str, storage) -> None:
    print(f"--- {label} ---")
    device = storage.active_device
    if device is None:
        print("  No active storage device reporting.")
        return
    print(f"  device={device.device_name!r} volume={device.volume}")
    print(f"  total space:     {_format_bytes(device.total_space)}")
    print(f"  remaining space: {_format_bytes(device.remaining_space)}")
    print(f"  remaining time:  {_format_seconds(device.remaining_record_time)}")
    print(f"  clip count:      {device.clip_count}")


async def run(args: argparse.Namespace) -> int:
    async with RestCameraSession(args.host, args.model_key, args.firmware) as session:
        baseline = await session.storage_state()
        _print_storage("BEFORE wait_for_low_storage (GET /media/workingset)", baseline)

        print(f"\n=== Waiting up to {args.timeout}s for low storage ===")
        print(f"min_record_time_s={args.min_record_time_s}, min_space_bytes={args.min_space_bytes}")
        print("Start a real recording now (another terminal / camera body) if you want to")
        print("actually cross the threshold before the timeout elapses.\n")

        start = time.monotonic()
        low = await session.wait_for_low_storage(
            min_record_time_s=args.min_record_time_s,
            min_space_bytes=args.min_space_bytes,
            timeout=args.timeout,
        )
        elapsed = time.monotonic() - start

        print(f"\nwait_for_low_storage() returned {low} after {elapsed:.1f}s")
        print("(True = low storage observed; False = timeout elapsed, storage stayed healthy)\n")

        if session.last_known_storage is not None:
            _print_storage("AFTER (last_known_storage)", session.last_known_storage)
        else:
            print("last_known_storage is still None — no /media/workingset event ever arrived.")

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify RestCameraSession.wait_for_low_storage() against real hardware."
    )
    parser.add_argument("--host", required=True, help="Camera hostname or IP.")
    parser.add_argument("--model-key", required=True)
    parser.add_argument("--firmware", required=True)
    parser.add_argument(
        "--min-record-time-s",
        type=float,
        default=None,
        help="Treat the active device as low once remaining_record_time drops to or below this.",
    )
    parser.add_argument(
        "--min-space-bytes",
        type=int,
        default=None,
        help="Treat the active device as low once remaining_space drops to or below this.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        help="Seconds to wait for a low-storage event before giving up. Default: 1800 (30 min).",
    )
    args = parser.parse_args()
    if args.min_record_time_s is None and args.min_space_bytes is None:
        parser.error("at least one of --min-record-time-s / --min-space-bytes is required")
    return args


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(run(parse_args())))
