"""
tools/rest/diagnose_timeline.py
==================================
Diagnostic for a real failure `examples/rest_playback.py`'s `select_clip()`
step hit twice in a row on a freshly-reformatted 128GB card, `POCKET_6K_G2
v8.6`, 2026-08-05: `POST /timelines/0/add {"clips": [{"clipUniqueId": 1}]}`
succeeded (no error), but `GET /timelines/0` came back completely empty
(`{"clips": []}`) on every read within `select_clip()`'s 5s poll budget —
not "other clips but not mine", genuinely empty. Every earlier confirmed
run of this same write path was on a card with more clips already on it;
this is the first attempt against a freshly-reformatted card with very
few clips, and the first data point either way on whether that's the
relevant variable.

`select_clip()`'s own 5s poll timeout gives up too fast to tell us
anything beyond "didn't happen within 5s" — this tool removes that limit
and prints every poll, so a slow-but-eventually-correct timeline and a
genuinely-stuck-empty one look different in the output instead of both
just failing the same way.

WHAT THIS DOES, AND WHY IT REACHES INTO A PRIVATE ATTRIBUTE
-------------------------------------------------------------
Uses `RestCameraSession` for every public read verb (`clips()`,
`get_format()`, `timeline_clip_ids()`), then calls
`session._rest_client.post(TIMELINE_ADD_PATH, ...)` directly for the one
write this investigates — there is no public wrapper around just the
`POST` half of what `select_clip()` does (it always follows through with
its own bounded poll). Reaching into `_rest_client` here is deliberate:
this tool's entire purpose is inspecting exactly what `select_clip()`
does internally, slower and with full visibility, not building new public
surface.

WHAT THIS CHANGES ON THE CAMERA
----------------------------------
The same one write `select_clip()` itself makes: `DELETE /timelines/0`
(expected `501`, handled the same defensive way `select_clip()` already
does) then `POST /timelines/0/add`. No format switch is attempted — this
tool never calls `set_camera_format()`, so if the target clip's format
doesn't match the camera's current one, the POST is sent anyway and the
mismatch itself becomes visible in the output rather than being silently
worked around first.

Usage:
    python tools/rest/diagnose_timeline.py \\
        --host pocket-cinema-camera-6k-g2.local \\
        --model-key POCKET_6K_G2 --firmware v8.6

    # Target a specific clip instead of clips()[0]:
    python tools/rest/diagnose_timeline.py \\
        --host pocket-cinema-camera-6k-g2.local \\
        --model-key POCKET_6K_G2 --firmware v8.6 --clip-id 1

    # Widen the poll window past select_clip()'s 5s default:
    python tools/rest/diagnose_timeline.py \\
        --host pocket-cinema-camera-6k-g2.local \\
        --model-key POCKET_6K_G2 --firmware v8.6 --poll-timeout 30

    # --skip-post: the decisive follow-up (docs/rest/session.md's select_clip()
    # section, finding #7's open question). Switches the camera's format to
    # match the target clip via set_camera_format() -- the same resolution
    # this tool otherwise deliberately skips -- then reads GET /timelines/0
    # immediately, with NO DELETE and NO POST ever sent. If the target clip
    # already appears, the format switch alone populates the timeline and
    # POST /timelines/0/add may never have mattered in any confirmed run to
    # date; if it doesn't appear, POST is doing real work after all.
    python tools/rest/diagnose_timeline.py \\
        --host pocket-cinema-camera-6k-g2.local \\
        --model-key POCKET_6K_G2 --firmware v8.6 --clip-id 16 --skip-post
"""

from __future__ import annotations

import argparse
import asyncio
import time

from bmd_camera import BMDVerificationError, RestCameraSession
from bmd_camera.exceptions import BMDConnectionError, BMDStorageError, BMDUnsupportedError
from bmd_camera.rest.exceptions import BMDRestError
from bmd_camera.rest.mapping import resolve_ble_codec_name
from bmd_camera.rest.session import (
    TIMELINE_ADD_PATH,
    TIMELINE_PATH,
    Clip,
    _parse_video_format,
    _resolution_name_for_dimensions,
)

# The raw RestClient calls below bypass select_clip()'s own error handling
# deliberately (that's the point of this tool) — these are the specific
# exceptions RestClient.delete()/.post() can raise, per its status-code
# contract (docs/rest/transport.md).
_REST_WRITE_ERRORS = (BMDUnsupportedError, BMDRestError, BMDConnectionError)


async def _run_skip_post(session: RestCameraSession, target: Clip, target_id: int) -> int:
    """The decisive test finding #7 (docs/rest/session.md) leaves open:
    switch format to match `target` via the same resolution path
    `select_clip()` uses internally, then read `GET /timelines/0`
    immediately — no `DELETE`, no `POST`, ever. If `target_id` already
    appears, the format switch alone populated the timeline."""
    parsed = _parse_video_format(target.video_format)
    if parsed is None:
        print(f"\nclip_unique_id={target_id}'s videoFormat {target.video_format!r} unparseable.")
        return 1
    width, height, fps_str = parsed

    ble_pair = resolve_ble_codec_name(session.profile.rest.format_names, target.codec)
    if ble_pair is None:
        print(f"\ncodec {target.codec!r} has no confirmed reverse mapping in this profile.")
        return 1
    family, variant = ble_pair

    resolution_name = _resolution_name_for_dimensions(session.profile.resolutions, width, height)
    if resolution_name is None:
        print(f"\n{width}x{height} has no matching entry in this profile's resolutions table.")
        return 1

    print(
        f"\n--- set_camera_format({family!r}, {variant!r}, {resolution_name!r}, {fps_str!r}) "
        f"(no DELETE, no POST) ---"
    )
    try:
        await session.set_camera_format(family, variant, resolution_name, fps_str)
        print("  confirmed")
    except (BMDUnsupportedError, BMDVerificationError, ValueError) as exc:
        print(f"  {type(exc).__name__}: {exc}")
        return 1

    after = await session.timeline_clip_ids()
    print(f"\n--- GET {TIMELINE_PATH} immediately after the switch, no write in between ---")
    print(f"  {after}")

    print("\n=== Result ===")
    if target_id in after:
        print(
            f"clip_unique_id={target_id} already appears — the format switch alone "
            "populated the timeline, with no POST /timelines/0/add ever sent."
        )
    else:
        print(
            f"clip_unique_id={target_id} does NOT appear — the format switch alone was "
            "not sufficient; POST /timelines/0/add does real work after all."
        )
    return 0


async def run(args: argparse.Namespace) -> int:
    async with RestCameraSession(args.host, args.model_key, args.firmware) as session:
        try:
            clips = await session.clips()
        except BMDStorageError as exc:
            print(f"clips(): {exc}")
            return 1
        if not clips:
            print("No clips on the card.")
            return 1

        print(f"--- clips() ({len(clips)} total) ---")
        for clip in clips:
            print(
                f"  clip_unique_id={clip.clip_unique_id} codec={clip.codec!r} "
                f"video_format={clip.video_format!r} file_path={clip.file_path!r}"
            )

        target_id = args.clip_id if args.clip_id is not None else clips[0].clip_unique_id
        matches = [c for c in clips if c.clip_unique_id == target_id]
        if not matches:
            print(f"\nclip_unique_id={target_id} not found in clips() above.")
            return 1
        target = matches[0]
        print(f"\nTargeting clip_unique_id={target_id}: {target.codec!r} {target.video_format!r}")

        fmt = await session.get_format()
        width, height = fmt.record_resolution
        print(
            f"\n--- get_format() (no switch attempted by this tool) ---\n"
            f"  {fmt.codec} @ {width}x{height}p{fmt.frame_rate}"
        )

        before = await session.timeline_clip_ids()
        print(f"\n--- GET {TIMELINE_PATH} before any write ---\n  {before}")

        if args.skip_post:
            return await _run_skip_post(session, target, target_id)

        endpoint = session.profile.rest_endpoint(TIMELINE_PATH)
        if endpoint is None or not endpoint.supported:
            print(f"\n{TIMELINE_PATH} not confirmed in this profile — cannot proceed.")
            return 1

        print(f"\n--- DELETE {TIMELINE_PATH} ---")
        try:
            await session._rest_client.delete(TIMELINE_PATH)
            print("  succeeded (unexpected — every prior run got 501 here)")
        except _REST_WRITE_ERRORS as exc:
            print(f"  {type(exc).__name__}: {exc}")

        print(f"\n--- POST {TIMELINE_ADD_PATH} {{'clips': [{{'clipUniqueId': {target_id}}}]}} ---")
        try:
            await session._rest_client.post(
                TIMELINE_ADD_PATH, {"clips": [{"clipUniqueId": target_id}]}
            )
            print("  succeeded (no error raised)")
        except _REST_WRITE_ERRORS as exc:
            print(f"  {type(exc).__name__}: {exc}")
            return 1

        print(f"\n--- Polling GET {TIMELINE_PATH} every 1s for up to {args.poll_timeout}s ---")
        start = time.monotonic()
        found_at = None
        last_ids: tuple[int, ...] = ()
        while time.monotonic() - start < args.poll_timeout:
            elapsed = time.monotonic() - start
            last_ids = await session.timeline_clip_ids()
            print(f"  t={elapsed:5.1f}s  {last_ids}")
            if target_id in last_ids:
                found_at = elapsed
                break
            await asyncio.sleep(1.0)

        print("\n=== Result ===")
        if found_at is not None:
            print(f"clip_unique_id={target_id} appeared after {found_at:.1f}s: {last_ids}")
        else:
            print(
                f"clip_unique_id={target_id} never appeared within {args.poll_timeout}s. "
                f"Last read: {last_ids}"
            )

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnose /timelines/0/add's behavior beyond select_clip()'s 5s poll budget."
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--model-key", required=True)
    parser.add_argument("--firmware", required=True)
    parser.add_argument(
        "--clip-id",
        type=int,
        default=None,
        help="clip_unique_id to target. Default: clips()[0], matching rest_playback.py.",
    )
    parser.add_argument(
        "--poll-timeout",
        type=float,
        default=30.0,
        help="Seconds to poll GET /timelines/0 for, well past select_clip()'s 5s default.",
    )
    parser.add_argument(
        "--skip-post",
        action="store_true",
        help="Switch format to match the target clip, then read the timeline with no "
        "DELETE/POST at all — isolates whether the format switch alone populates it.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
