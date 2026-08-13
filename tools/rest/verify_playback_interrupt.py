"""
tools/rest/verify_playback_interrupt.py
==========================================
Real-hardware verification for `RestCameraSession.playback_interrupted` /
`wait_for_playback_interrupt()` (Phase 8 item 2, part 2,
`docs/rest/session.md`'s Phase 8 item 2 section) — the camera-initiated
playback interrupt detection built alongside `/transports/0/play` and
`/transports/0/stop`'s Part 1 WS-push-shape confirmation
(`tools/rest/watch_events.py`, `POCKET_6K_G2 v8.6`, 2026-08-05). This is the
one piece of that feature that has only run against the injected-fake unit
test suite so far.

WHAT THIS TOOL DOES
--------------------
Two phases, in order:

1. **Self-requested sanity check** (the negative case): `select_clip()` ->
   `enter_playback()` -> `play()` -> `pause()` -> `play()` -> `stop()`, with
   `session.playback_interrupted` checked after every step. None of these
   are expected to set it — they are exactly the writes
   `_playback_write_in_flight`/`_transport_mode_write_in_flight` exist to
   shield from being misread as a camera-initiated interrupt. A `set()`
   here means the in-flight guard has a real bug, not that the camera did
   anything unusual.

2. **Camera-initiated interrupt** (the positive case, the one no unit test
   can exercise): re-selects the clip (`stop()`'s `exit_playback()` alias
   reverts the camera's format — see `exit_playback()`'s own docstring —
   so this is necessary before playback can resume), enters playback,
   starts `play()`, then prompts you to pull the SD card or press
   stop/pause directly on the camera body. Waits up to `--interrupt-timeout`
   seconds via `wait_for_playback_interrupt()` and reports what it saw:
   the return value, `last_known_play`/`last_known_stop` (Part 1's
   independent corroborating signal), and a fresh `GET /transports/0`
   readback of the camera's own mode.

Prints a status line after every write so a failure is attributable to a
specific step, the same discipline `examples/rest_playback.py` uses.

Leaves the camera in preview mode on exit — `exit_playback()` is always
attempted in a `finally`, even if phase 2's wait fails or times out
(logged, not raised, since cleanup should not mask the tool's own result).

USAGE
-----
    python tools/rest/verify_playback_interrupt.py \\
        --host pocket-cinema-camera-6k-g2.local \\
        --model-key POCKET_6K_G2 --firmware v8.6 \\
        --clip-id 1 --interrupt-timeout 120

Omit --clip-id to use the first clip `clips()` reports. Increase
--interrupt-timeout if you need more time to physically reach the camera
after `play()` starts.

WHAT THIS CHANGES ON THE CAMERA: switches recording format (via
`select_clip()`, if the requested clip doesn't already match), enters and
exits playback mode, and starts/stops playback — the same footprint
`examples/rest_playback.py` already has. Does not record, capture a photo,
or touch storage.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from bmd_camera import BMDUnsupportedError, BMDVerificationError, RestCameraSession
from bmd_camera.exceptions import BMDConnectionError, BMDStorageError
from bmd_camera.rest.exceptions import BMDRestError

logger = logging.getLogger(__name__)


async def _step(label: str, coro) -> bool:
    print(f"--- {label} ---")
    try:
        await coro
    except (BMDVerificationError, BMDUnsupportedError, BMDStorageError, ValueError) as exc:
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        return False
    print("  OK")
    return True


def _check_not_interrupted(session: RestCameraSession, after: str) -> bool:
    if session.playback_interrupted.is_set():
        print(f"  UNEXPECTED: playback_interrupted is set after {after} — in-flight guard bug")
        return False
    print(f"  playback_interrupted still clear after {after} (expected)")
    return True


async def run(args: argparse.Namespace) -> int:
    async with RestCameraSession(args.host, args.model_key, args.firmware) as session:
        clips = await session.clips()
        if not clips:
            print("No clips reported by GET /clips/list — nothing to select.")
            return 1
        if args.clip_id is not None:
            matches = [c for c in clips if c.clip_unique_id == args.clip_id]
            if not matches:
                print(f"clip_unique_id={args.clip_id} not found in GET /clips/list")
                return 1
            clip = matches[0]
        else:
            clip = clips[0]
        print(f"Using clip_unique_id={clip.clip_unique_id} ({clip.file_path})\n")

        ok = True
        print("=== Phase 1: self-requested sanity check (expect no interrupt) ===")
        ok &= await _step("select_clip", session.select_clip(clip.clip_unique_id))
        ok &= await _step("enter_playback", session.enter_playback())
        ok &= await _step("play", session.play())
        ok &= _check_not_interrupted(session, "play()")
        ok &= await _step("pause", session.pause())
        ok &= _check_not_interrupted(session, "pause()")
        ok &= await _step("play (again)", session.play())
        ok &= _check_not_interrupted(session, "play() again")
        ok &= await _step("stop", session.stop())
        ok &= _check_not_interrupted(session, "stop()")
        if session._in_playback:
            print("  UNEXPECTED: _in_playback still True after stop()")
            ok = False

        if not ok:
            print("\nPhase 1 failed — not proceeding to phase 2. Fix the sanity check first.")
            return 1

        try:
            print("\n=== Phase 2: camera-initiated interrupt (the real test) ===")
            ok &= await _step(
                "select_clip (re-select after stop()'s format revert)",
                session.select_clip(clip.clip_unique_id),
            )
            ok &= await _step("enter_playback", session.enter_playback())
            ok &= await _step("play", session.play())
            if not ok:
                print("\nCould not get back into playback — aborting phase 2.")
                return 1

            print(
                f"\nPlaying now. Pull the SD card, or press stop/pause directly on the "
                f"camera body, within the next {args.interrupt_timeout:.0f}s.\n"
            )
            start = time.monotonic()
            interrupted = await session.wait_for_playback_interrupt(timeout=args.interrupt_timeout)
            elapsed = time.monotonic() - start

            print(f"\nwait_for_playback_interrupt() returned {interrupted} after {elapsed:.1f}s")
            print("(True = interrupt observed; False = timeout elapsed, nothing detected)")
            print(
                f"last_known_play={session.last_known_play} "
                f"last_known_stop={session.last_known_stop}"
            )

            try:
                fmt_check = await session._rest_client.get("/transports/0")
                print(f"GET /transports/0 -> {fmt_check}")
            except (BMDConnectionError, BMDUnsupportedError, BMDRestError) as exc:
                print(f"GET /transports/0 failed: {exc}")

            if not interrupted:
                print(
                    "\nNo interrupt detected within the timeout — either nothing was done to "
                    "the camera, or the interrupt mechanism has a real gap. Re-run with a "
                    "longer --interrupt-timeout and interrupt sooner if this was just timing."
                )
                return 1
            print("\nInterrupt detected as expected.")
            return 0
        finally:
            print("\n=== Cleanup ===")
            try:
                await session.exit_playback()
                print("exit_playback() OK")
            except BMDVerificationError as exc:
                logger.warning("exit_playback() cleanup failed: %s", exc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify RestCameraSession.playback_interrupted / "
            "wait_for_playback_interrupt() against real hardware."
        )
    )
    parser.add_argument("--host", required=True, help="Camera hostname or IP.")
    parser.add_argument("--model-key", required=True)
    parser.add_argument("--firmware", required=True)
    parser.add_argument(
        "--clip-id",
        type=int,
        default=None,
        help="Clip.clip_unique_id to select. Defaults to the first clip in GET /clips/list.",
    )
    parser.add_argument(
        "--interrupt-timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for a camera-initiated interrupt after play() starts. Default: 120.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(run(parse_args())))
