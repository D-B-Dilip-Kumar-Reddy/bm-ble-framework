"""
Exercise RestCameraSession's Phase 7 playback surface — select a clip from
the camera's own media, enter playback, play/pause/seek/shuttle, then leave
playback mode again. Entirely new capability BLE never reached
(docs/rest/transport.md's "New capability REST brings").

WHAT THIS SCRIPT CHANGES ON THE CAMERA: may switch the camera's recording
format (select_clip() does this automatically when needed — see below),
switches to playback mode, and scrubs through footage. Does not record or
delete anything, but does leave the camera in playback mode (and possibly a
different format than it started in) if a step raises partway through —
press stop/exit on the camera body if that happens, and check the format
before your next recording.

VERIFICATION IS BY EYE, NOT ONLY BY THIS SCRIPT — read this before running
--------------------------------------------------------------------------------
Every write here is verified the same dual-check way as every other
RestCameraSession write (a WS propertyValueChanged event primary, a GET
readback secondary, BMDVerificationError if neither confirms).
/transports/0/playback's body ({"type", "loop", "singleClip", "speed",
"position"}) and /timelines/0/add's POST body ({"clips":
[{"clipUniqueId": ...}]}) are both real-hardware-confirmed (POCKET_6K_PRO
v8.6, 2026-08-04) — see RestCameraSession._put_playback's and
select_clip()'s own docstrings. select_clip() itself replaces an earlier
set_timeline(clip_unique_ids: list[int]) design that direct Postman
debugging disproved outright: the camera has no concept of a caller-curated
playlist. Requesting one clip while the camera's format matched that clip
produced a GET /timelines/0 readback of every clip sharing that format —
seven clips, not one — confirmed two independent ways (the REST readback
itself, and the camera's own on-screen playback view showing "CLIP 1/7" for
the same group). select_clip() is built around that reality: it switches
format to match the requested clip, then confirms the clip appears
somewhere in the resulting (whole-format-group) timeline — not that it's
alone there.

This exact combination — the format check, the switch when needed, and the
timeline sync — is now real-hardware-confirmed: a full run of this script
(select_clip through exit_playback, all nine steps) passed clean on both
POCKET_6K_G2 and POCKET_6K_PRO v8.6, 2026-08-04, every step's own
dual-check confirming. That run's clip already matched the camera's
current format on both cameras, so it didn't exercise select_clip()'s
set_camera_format() branch specifically — a clip whose format doesn't
match remains the next case to confirm. This script's own dual-check
passing is real evidence the writes and their confirmation channels work;
it is not the same as an operator watching the footage actually play, so
still watch the camera's own screen for that ground truth, the same way
docs/rest/transport.md's own Phase 7 note says to.

CAVEAT ADDED 2026-08-05, READ BEFORE TRUSTING "the POST selected the clip":
tools/rest/diagnose_timeline.py found that POST /timelines/0/add is a
no-op when the target clip's format doesn't already match the camera's
live format — no error, no timeline change, ever. Every confirmed success
above (this script's own run included) switched format to match the
target clip before POSTing, so none of them can actually distinguish "the
POST did the selecting" from "the format switch alone already would have
populated the timeline, POST or not." See docs/rest/session.md's
select_clip() section, finding #7 — this script's own passing runs are
still real evidence the end-to-end sequence works, just not proof of what
specifically inside it is doing the work.

SENSOR-RESOLUTION AMBIGUITY RETRY, ADDED 2026-08-05
------------------------------------------------------
`select_clip()` has no `sensor_resolution` parameter of its own (see its
docstring's third gap) — real case, `POCKET_6K_G2 v8.6`: `ProRes:HQ` at
`1920x1080p25` pairs with three (`2880x1512`, `5376x3024`, `6144x3456`),
and `select_clip()` raises `BMDUnsupportedError` rather than guess.
Deliberately kept a library-level restriction, not fixed there — an
example script is the right place to compose a retry around a strict
library call, not the library itself (design principle 7: an unsupported
operation raises immediately, no silent guessing).

`_select_clip_trying_all_sensor_resolutions()` below: try `select_clip()`
plain first (the fast, unambiguous path). If it raises `BMDUnsupportedError`,
independently re-derive the real candidate `sensorResolution` values from
`supported_formats()` (never by parsing the exception string) using the
same filter `set_camera_format()` applies internally. If there's genuinely
more than one, call `set_camera_format()` directly with each candidate in
turn — `select_clip()`'s own format comparison never checks
`sensorResolution` (only codec/resolution/fps), so once a candidate is set,
calling `select_clip()` again sees the format as already matching and skips
straight to its `POST`/poll, no re-triggering the ambiguity check. Stops at
the first candidate whose `select_clip()` call succeeds. If the original
error wasn't really the ambiguity case (0 or 1 real candidates), does not
retry — that would just repeat a failure retrying can't fix. Not yet run
against real hardware.

Usage:
    python examples/rest_playback.py
"""

import asyncio
import logging
import sys

from bmd_camera import BMDUnsupportedError, BMDVerificationError, RestCameraSession
from bmd_camera.exceptions import BMDStorageError
from bmd_camera.rest.mapping import resolve_ble_codec_name
from bmd_camera.rest.session import Clip, _parse_video_format, _resolution_name_for_dimensions

HOST = "pocket-cinema-camera-6k-g2.local"
MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v8.6"
# HOST = "pocket-cinema-camera-6k-pro.local"
# MODEL_KEY = "POCKET_6K_PRO"
# FIRMWARE = "v8.6"

PLAY_DURATION_S = 3.0
PAUSE_BETWEEN_STEPS_S = 2.0
# stop() is currently an alias for exit_playback() (see session.py's docstring for why) —
# both send the identical PUT /transports/0 {"mode": "InputPreview"}. Whatever's visible on
# the camera screen happens at the "stop" step; this longer pause just gives an operator
# time to actually look before the (functionally redundant) "exit_playback" step re-fires
# the same call.
STOP_TO_EXIT_PAUSE_S = 8.0


async def _print_format(session: RestCameraSession, label: str) -> None:
    fmt = await session.get_format()
    width, height = fmt.record_resolution
    print(f"{label}: {fmt.codec} @ {width}x{height}p{fmt.frame_rate}")


async def _select_clip_trying_all_sensor_resolutions(
    session: RestCameraSession, clip: Clip
) -> None:
    """`select_clip(clip.clip_unique_id)`, but on the one gap its own
    docstring names and declines to work around — a `(codec, resolution,
    fps)` combination pairing with more than one `sensorResolution` — tries
    every real candidate `set_camera_format()` reports instead of giving up
    after the first `BMDUnsupportedError`. See this module's own docstring
    for why this lives here and not in `select_clip()` itself."""
    try:
        await session.select_clip(clip.clip_unique_id)
        return
    except BMDUnsupportedError as exc:
        original_exc = exc
        print(f"  select_clip() raised {type(exc).__name__}: {exc}")
        print("  checking whether this is the sensor-resolution ambiguity case...")

    parsed = _parse_video_format(clip.video_format)
    if parsed is None:
        raise BMDUnsupportedError(
            f"clip_unique_id={clip.clip_unique_id}'s videoFormat {clip.video_format!r} "
            "doesn't match the confirmed '<width>x<height>p<fps>' shape"
        )
    width, height, fps_str = parsed

    formats = await session.supported_formats()
    candidates = [
        f.sensor_resolution
        for f in formats
        if f.record_resolution == (width, height)
        and clip.codec in f.codecs
        and fps_str in f.frame_rates
    ]
    if len(candidates) <= 1:
        # Not the ambiguity case (or genuinely unsupported) — retrying with
        # a sensor_resolution wouldn't change anything. Let the original
        # failure stand rather than silently reporting a different one.
        raise original_exc
    print(f"  {len(candidates)} candidate sensorResolution values: {candidates}")

    ble_pair = resolve_ble_codec_name(session.profile.rest.format_names, clip.codec)
    if ble_pair is None:
        raise BMDUnsupportedError(
            f"codec {clip.codec!r} has no confirmed reverse mapping in this profile"
        )
    family, variant = ble_pair
    resolution_name = _resolution_name_for_dimensions(session.profile.resolutions, width, height)
    if resolution_name is None:
        raise BMDUnsupportedError(
            f"{width}x{height} has no matching entry in this profile's resolutions table"
        )

    last_exc: Exception | None = None
    for sensor_resolution in candidates:
        print(f"  trying sensor_resolution={sensor_resolution}...")
        try:
            await session.set_camera_format(
                family, variant, resolution_name, fps_str, sensor_resolution=sensor_resolution
            )
            await session.select_clip(clip.clip_unique_id)
        except (BMDUnsupportedError, BMDVerificationError) as exc:
            print(f"    failed: {type(exc).__name__}: {exc}")
            last_exc = exc
            continue
        print(f"  succeeded with sensor_resolution={sensor_resolution}")
        return

    raise BMDUnsupportedError(
        f"clip_unique_id={clip.clip_unique_id}: none of {len(candidates)} candidate "
        f"sensorResolution values worked ({candidates}); last error: {last_exc}"
    )


async def _step(label: str, action) -> bool:
    try:
        await action()
    except BMDVerificationError as exc:
        print(f"{label} NOT confirmed: {exc}")
        return False
    except (BMDUnsupportedError, ValueError) as exc:
        print(f"{label} not attempted: {exc}")
        return False
    print(f"{label} confirmed ✓ — check the camera screen")
    return True


async def main() -> int:
    results: list[tuple[str, bool]] = []

    async with RestCameraSession(HOST, MODEL_KEY, FIRMWARE) as session:
        try:
            clips = await session.clips()
        except BMDStorageError as exc:
            print(f"clips(): {exc}")
            return 1
        if not clips:
            print("No clips on the card — record at least one short clip first.")
            return 1

        target_clip = clips[0]
        print(f"Selecting clip_unique_id={target_clip.clip_unique_id} ({target_clip.file_path})")
        await _print_format(session, "Format before select_clip")

        steps: list[tuple[str, object]] = [
            (
                "select_clip",
                lambda: _select_clip_trying_all_sensor_resolutions(session, target_clip),
            ),
            ("enter_playback", session.enter_playback),
            ("play", session.play),
        ]
        for label, action in steps:
            print(f"\n=== {label} ===")
            ok = await _step(label, action)
            results.append((label, ok))
            if not ok:
                break
            await asyncio.sleep(PLAY_DURATION_S if label == "play" else PAUSE_BETWEEN_STEPS_S)

        if all(ok for _, ok in results):
            for label, action in [
                ("pause", session.pause),
                ("seek(0)", lambda: session.seek(0)),
                ("shuttle(2.0) forward", lambda: session.shuttle(2.0)),
                ("shuttle(-1.0) backward", lambda: session.shuttle(-1.0)),
                ("stop", session.stop),
                ("exit_playback", session.exit_playback),
            ]:
                print(f"\n=== {label} ===")
                ok = await _step(label, action)
                results.append((label, ok))
                if not ok:
                    break
                pause_s = STOP_TO_EXIT_PAUSE_S if label == "stop" else PAUSE_BETWEEN_STEPS_S
                await asyncio.sleep(pause_s)

        await _print_format(session, "Format after exit_playback")

    print("\n=== Summary ===")
    for label, ok in results:
        print(f"  {'OK    ' if ok else 'FAILED'}  {label}")

    if not all(ok for _, ok in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
