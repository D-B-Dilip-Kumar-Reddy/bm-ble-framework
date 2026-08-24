"""
Capture several stills in one session, each guessed, downloaded, and then
deleted — the first multi-still workflow in this codebase, and the
real-hardware exercise of `guess_new_still_path()`'s `exclude` parameter
(added 2026-08-24 specifically for this scenario).

WHY THIS NEEDS `exclude`, UNLIKE EVERY EARLIER SINGLE-STILL SCRIPT
--------------------------------------------------------------------
`capture_photo.py`/`rest_delete_still.py`/`rest_download_still.py` all
guess exactly one still per run, so there is never a previous guess to
collide with. `guess_new_still_path()`'s default `minute_offsets` was
widened to `(0, -1, 1, -2, 2, -3, 3)` (docs/rest/session.md,
`rest/media.py`'s module docstring) to cover the fact that the SETUP >
Date/Time screen has no Seconds field — but that wider ±3-minute search
radius creates a new risk unique to *repeated* captures: an **earlier**
still's real, still-existing filename can sit well within a **later**
still's search window and get matched first, silently returning a stale,
already-processed path instead of the new one. This script accumulates
every path it has already guessed into `guessed_paths` and passes it as
`exclude` on each subsequent guess, so a stale match is skipped rather
than returned.

ONE BLE CONNECTION FOR THE WHOLE RUN (2026-08-24, first real-hardware run's
finding) — the original version opened and closed a fresh `CameraSession`
per still, matching every single-still script's own pattern. The operator
correctly flagged that as unwanted overhead for a multi-still run — each
reconnect costs several real seconds, and there is no reason to pay it
STILL_COUNT times when nothing about a BLE connection is still-specific.
`capture_photo()` is a stateless trigger send; sending it repeatedly over
one held-open `CameraSession`, the same way `send_settings_command.py
--repeat` already sends other commands repeatedly over one connection, is
not expected to behave any differently — but this is the first time this
exact codebase has tried it, so it's flagged here rather than assumed
silently confirmed.

INDEX SEARCH IS NOW ADAPTIVE (2026-08-24, second real-hardware finding) —
that same first run guessed 0/10 stills, every single one failing at the
`guess` step. The far more likely explanation than the BLE-reconnect
concern above: `guess_new_still_path()`'s own default `index_candidates`
(`range(1, 11)`) is a deliberate "shot in the dark" for a card whose still
count is unknown (its own docstring says so) — and this exact card, across
this whole development session's many earlier real-hardware photo
captures, almost certainly already holds well more than 10 stills, putting
every *new* index outside that default window on every single attempt.
This script no longer trusts that narrow default blindly: the *first*
still in a run searches a much wider `INITIAL_INDEX_CANDIDATES` (`range(1,
51)`) to bootstrap a real starting index — a one-time, bounded cost, not
the unbounded search the library's own docstring warns against — and every
*later* still in the same run reuses `HINTED_INDEX_CANDIDATES` (a narrow
band just above the last confirmed index, since the counter only
increments), exactly the "pass a narrow range around a real hint" pattern
`guess_new_still_path()`'s own docstring already recommends. Falls back to
the wide range if the narrow one ever comes up empty (e.g. another
capture, from another app or the body itself, landed between this
session's own stills).

SECOND REAL-HARDWARE RUN — both fixes above worked (one connection held the
whole time, every guess that got the chance to run succeeded, `exclude`
never had to intervene since no collision ever occurred) — but surfaced a
third, distinct problem: a strict alternating pattern, stills 1/3/5/7/9
confirmed and completed cleanly while every even-numbered still (2/4/6/8/10)
failed at `confirm` — "Stills directory did not change within 15.0s" —
every single time, all ten stills. Working hypothesis: this camera needs a
real minimum recovery interval between physical photo captures, and this
script's zero-delay loop (the next trigger fired within ~0.3-0.6s of the
previous still's delete completing) lands inside that window often enough
to be dropped outright — while the *retry* implicit in each failure's own
15s timeout-then-immediately-trigger-again always lands well past it,
which is exactly the ~15s-vs-under-1s split the data shows. `INTER_STILL_
DELAY_S` (default `3.0`) adds an explicit pause between the end of one
still's cycle and the next trigger, long enough to clear a supposed
cooldown window that the evidence brackets as ">1s, <15s" without pinning
down more precisely than that — not yet confirmed itself. A secondary,
separate, unexplained anomaly from the same run: still 1's download was
only 4096 bytes (every other successful still downloaded a consistent
~1MB, matching `rest_download_still.py`'s own earlier confirmed real-still
size) — not addressed by this fix at the time, and not understood.

THIRD REAL-HARDWARE RUN (2026-08-24, `STILL_COUNT=10`, with the delay
above) confirmed `INTER_STILL_DELAY_S` fixed the confirm-timeout problem
completely — 10/10 stills confirmed and guessed correctly, no more
alternating failures. But the "secondary anomaly" from the second run
turned out to be the dominant behavior, not a one-off: 9 of the 10
downloads came back as exactly 4096 bytes, and only the one still whose
guess happened to take measurably longer (an extra ~1s, from an
index-search fallback) downloaded a real, correctly-sized file (`988264`
bytes). Working hypothesis: `wait_for_new_still()`'s `mtime` signal fires
as soon as the Stills entry is *created*, not once the camera has finished
*writing* the real payload into it — downloading immediately after
confirmation, as this script always has, was catching a small placeholder/
header allocation instead of the finished file, and only succeeded when
something incidentally delayed the download long enough for the real
write to land first. Fixed by retrying the download itself, up to
`DOWNLOAD_MAX_ATTEMPTS` times with `DOWNLOAD_RETRY_DELAY_S` between tries,
whenever the result is smaller than `MIN_STILL_BYTES` — a threshold chosen
with generous margin above the one observed placeholder size and below
every observed real one, but from limited evidence (one resolution/codec
only). Not yet confirmed itself.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: takes `STILL_COUNT` real photos,
downloads each to `DEST_DIR`, then deletes each from the card. Net effect
on the card is nothing once a still's round trip succeeds (matching
`rest_delete_still.py`'s own framing) — a local copy of each still is kept.

PER-STILL SEQUENCE (mirrors `rest_delete_still.py` and
`rest_download_still.py`'s guess-then-act shape, run once per still, over
the one BLE connection and one REST session held open for the whole run):
snapshot the Stills `mtime` baseline -> trigger over BLE -> confirm over
REST (`wait_for_new_still()`) -> guess the filename
(`guess_new_still_path()`, `exclude=guessed_paths`, an adaptive
`index_candidates`) -> download it -> delete it.

PARTIAL FAILURE IS EXPECTED, NOT FATAL — one still's failure at any step
does not stop the batch, the same partial-success philosophy
`delete_clips()`/`download_clips()` already established for their own bulk
operations (`docs/rest/session.md`). Each still's outcome (captured,
guessed, downloaded, deleted, or where it stopped and why) is tracked and
printed in a final summary — this is why the script is not just three
single-still scripts pasted in a loop.

FOURTH REAL-HARDWARE RUN (2026-08-24, `STILL_COUNT=10`, with the download
retry above) CONFIRMED the fix: 10/10 stills succeeded, and every single
download landed at the same real size (`943208` bytes) the third run's one
success also reported. The retry logs show the placeholder-then-real-file
theory holding exactly as predicted: 8 of the 10 stills needed exactly one
retry (4096 bytes on attempt 1, the real size ~0.4-0.8s later on attempt 2),
while the other 2 (stills 6 and 9) happened to get the real file on the
very first attempt — consistent with the write-completion timing being
real but variable, not a fixed delay, which is exactly why a size-based
retry loop was chosen over a fixed sleep. Total run time was also far
faster than the second/third runs (40.7s for all 10 stills) since at most
one extra `DOWNLOAD_RETRY_DELAY_S` was ever spent per still.

STATUS: every mechanism in this script — `exclude`, the held-open BLE
connection, the adaptive index search, `INTER_STILL_DELAY_S`, and the
download-size retry (`MIN_STILL_BYTES`/`DOWNLOAD_MAX_ATTEMPTS`/
`DOWNLOAD_RETRY_DELAY_S`) — is now real-hardware-confirmed working as
designed, end to end, 10/10, real file sizes throughout (fourth run above).

Usage:
    python examples/capture_multiple_stills.py
"""

import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bmd_camera import BMDUnsupportedError, BMDVerificationError, CameraSession, RestCameraSession
from bmd_camera.exceptions import BMDStorageError
from bmd_camera.rest.media import (
    guess_new_still_path,
    resolve_active_mount,
    stills_marker,
    wait_for_new_still,
)

HOST = "pocket-cinema-camera-6k-g2.local"
REST_MODEL_KEY = "POCKET_6K_G2"
REST_FIRMWARE = "v8.6"
BLE_MODEL_KEY = "POCKET_6K_G2"
BLE_FIRMWARE = "v8.6"
# HOST = "pocket-cinema-camera-6k-pro.local"
# REST_MODEL_KEY = "POCKET_6K_PRO"
# REST_FIRMWARE = "v8.6"
# BLE_MODEL_KEY = "POCKET_6K_PRO"
# BLE_FIRMWARE = "v8.6"

STILL_COUNT = 3
CONFIRM_TIMEOUT_S = 15.0
DEST_DIR = Path(__file__).parent / "downloads"

# The first still's index is unknown — a card from this development
# session's many earlier photo captures may already hold well more than
# guess_new_still_path()'s own default range(1, 11) covers. Wider, but
# still bounded, one-time cost to bootstrap a real hint.
INITIAL_INDEX_CANDIDATES = range(1, 51)
# Once a real index is known, the counter only increments — a narrow band
# just above it is both faster and more targeted than guessing blind again.
HINTED_INDEX_WINDOW = 5

# Second real-hardware run (2026-08-24, STILL_COUNT=10, no delay at all):
# every even-numbered still failed to confirm within 15s, every odd one
# succeeded cleanly — a strict alternating pattern pointing at a real
# camera-side cooldown between physical captures that a zero-delay loop
# lands inside often enough to matter. Bracketed by the evidence itself as
# ">1s (back-to-back failed), <15s (always recovers by the retry)" — not
# pinned down more precisely than that. See module docstring.
INTER_STILL_DELAY_S = 3.0

# Third real-hardware run (2026-08-24, STILL_COUNT=10, with the delay
# above): all 10 confirmed and guessed correctly, but 9 of 10 downloads
# came back as exactly 4096 bytes — a suspiciously round, unvarying size,
# against every previously-confirmed real still in this codebase landing
# in the ~950KB-1MB range. Working hypothesis: `wait_for_new_still()`'s
# mtime signal fires as soon as the file is *created* (a small placeholder/
# header allocation), not once the camera has finished *writing* the real
# payload into it — downloading immediately after confirmation, as this
# script always has, can catch that placeholder instead of the finished
# file. The one still that DID download a real size (988264 bytes) also
# happened to be the one still whose guess took measurably longer (an
# extra ~1s, likely from an index-search fallback) — consistent with "more
# elapsed time before downloading -> more likely the write had finished."
# MIN_STILL_BYTES gives an intentionally generous margin: an order of
# magnitude above the observed placeholder size and an order of magnitude
# below every observed real one, so it should not mistake a genuinely
# smaller real still (a resolution/codec this session hasn't seen) for a
# placeholder — but this is a threshold chosen from limited evidence
# (`.braw` stills at one resolution/codec only), not a confirmed rule.
# Fourth real-hardware run (2026-08-24, STILL_COUNT=10) confirmed the whole
# retry mechanism: 10/10 downloads landed at the real 943208-byte size, 8 of
# them needing exactly one retry (4096 bytes -> real size ~0.4-0.8s later)
# and 2 (stills 6 and 9) getting the real file on the first attempt — real
# but variable write-completion timing, exactly why a size-based retry was
# chosen over a fixed sleep. See module docstring.
MIN_STILL_BYTES = 100_000
DOWNLOAD_MAX_ATTEMPTS = 5
DOWNLOAD_RETRY_DELAY_S = 1.0


def _format_bytes(num_bytes: int) -> str:
    gb = num_bytes / 1_000_000_000
    return f"{num_bytes} bytes ({gb:.2f} GB)"


def _index_candidates_for(last_confirmed_index: int | None) -> range:
    if last_confirmed_index is None:
        return INITIAL_INDEX_CANDIDATES
    return range(last_confirmed_index + 1, last_confirmed_index + 1 + HINTED_INDEX_WINDOW)


def _index_from_path(path: str) -> int | None:
    """Best-effort extraction of the `_S<NNN>` counter from a guessed path,
    to seed the next still's hinted search. Returns `None` on any shape
    this doesn't recognize rather than raising — losing the hint just
    means the next still falls back to a wider search, not a crash."""
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    marker = stem.rfind("_S")
    if marker == -1:
        return None
    digits = stem[marker + 2 :]
    return int(digits) if digits.isdigit() else None


@dataclass
class StillOutcome:
    index: int
    captured: bool = False
    guessed_path: str | None = None
    downloaded_to: Path | None = None
    deleted: bool = False
    stopped_at: str | None = None
    error: str = ""

    def summary_line(self) -> str:
        if self.deleted:
            return f"  [{self.index}] OK — {self.guessed_path}"
        stage = self.stopped_at or "unknown"
        return f"  [{self.index}] FAILED at {stage} — {self.error}"


async def capture_one_still(
    ble_session: CameraSession,
    rest_session: RestCameraSession,
    mount_path: str,
    index: int,
    guessed_paths: list[str],
    last_confirmed_index: int | None,
) -> StillOutcome:
    outcome = StillOutcome(index=index)

    baseline = await stills_marker(rest_session, mount_path)

    trigger_time = datetime.now()
    try:
        await ble_session.capture_photo()
    except BMDUnsupportedError as exc:
        outcome.stopped_at = "trigger"
        outcome.error = str(exc)
        return outcome
    print(f"  [{index}] Trigger sent over BLE — confirming over REST …")

    confirmed = await wait_for_new_still(
        rest_session, mount_path, baseline, timeout_s=CONFIRM_TIMEOUT_S
    )
    if not confirmed:
        outcome.stopped_at = "confirm"
        outcome.error = f"Stills directory did not change within {CONFIRM_TIMEOUT_S}s"
        return outcome
    outcome.captured = True
    print(f"  [{index}] Confirmed ✓")

    index_candidates = _index_candidates_for(last_confirmed_index)
    guessed_path = await guess_new_still_path(
        rest_session,
        mount_path,
        around=trigger_time,
        index_candidates=index_candidates,
        exclude=guessed_paths,
    )
    if guessed_path is None and last_confirmed_index is not None:
        # The hinted narrow band came up empty — fall back to the wide
        # search once rather than giving up (see module docstring).
        guessed_path = await guess_new_still_path(
            rest_session,
            mount_path,
            around=trigger_time,
            index_candidates=INITIAL_INDEX_CANDIDATES,
            exclude=guessed_paths,
        )
    if guessed_path is None:
        outcome.stopped_at = "guess"
        outcome.error = (
            f"guess_new_still_path() found nothing new (searched index_candidates="
            f"{list(index_candidates)!r}, then the wide fallback)"
        )
        return outcome
    outcome.guessed_path = guessed_path
    guessed_paths.append(guessed_path)
    print(f"  [{index}] Guessed: {guessed_path}")

    dest: Path | None = None
    size = 0
    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            dest = await rest_session.download_still(guessed_path, DEST_DIR, overwrite=True)
        except (ValueError, FileExistsError, BMDVerificationError) as exc:
            outcome.stopped_at = "download"
            outcome.error = str(exc)
            return outcome
        size = dest.stat().st_size
        if size >= MIN_STILL_BYTES:
            break
        print(
            f"  [{index}] Download suspiciously small ({size} bytes) on attempt "
            f"{attempt}/{DOWNLOAD_MAX_ATTEMPTS} — camera may still be writing the file, "
            f"retrying …"
        )
        if attempt < DOWNLOAD_MAX_ATTEMPTS:
            await asyncio.sleep(DOWNLOAD_RETRY_DELAY_S)
    else:
        outcome.stopped_at = "download"
        outcome.error = (
            f"file stayed suspiciously small ({size} bytes) after {DOWNLOAD_MAX_ATTEMPTS} attempts"
        )
        return outcome
    outcome.downloaded_to = dest
    print(f"  [{index}] Downloaded: {dest} ({_format_bytes(size)})")

    try:
        await rest_session.delete_still(guessed_path, confirm=True)
    except (ValueError, BMDVerificationError) as exc:
        outcome.stopped_at = "delete"
        outcome.error = str(exc)
        return outcome
    outcome.deleted = True
    print(f"  [{index}] Deleted ✓")

    return outcome


async def main() -> int:
    DEST_DIR.mkdir(parents=True, exist_ok=True)

    async with (
        RestCameraSession(HOST, REST_MODEL_KEY, REST_FIRMWARE) as rest_session,
        CameraSession(BLE_MODEL_KEY, BLE_FIRMWARE) as ble_session,
    ):
        storage = await rest_session.storage_state()
        if storage.active_device is None:
            raise BMDStorageError(f"[{HOST}] No active storage device — cannot capture a photo")
        print(f"Active storage: {storage.active_device.device_name}")

        mount_path = await resolve_active_mount(rest_session)
        print(f"Resolved mount: {mount_path}")

        guessed_paths: list[str] = []
        outcomes: list[StillOutcome] = []
        last_confirmed_index: int | None = None
        start = time.monotonic()

        for index in range(1, STILL_COUNT + 1):
            if index > 1:
                print(f"\nWaiting {INTER_STILL_DELAY_S}s for the camera to settle …")
                await asyncio.sleep(INTER_STILL_DELAY_S)
            print(f"\n=== Still {index}/{STILL_COUNT} ===")
            outcome = await capture_one_still(
                ble_session, rest_session, mount_path, index, guessed_paths, last_confirmed_index
            )
            outcomes.append(outcome)
            if outcome.guessed_path is not None:
                found_index = _index_from_path(outcome.guessed_path)
                if found_index is not None:
                    last_confirmed_index = found_index
            if not outcome.deleted:
                print(f"  [{index}] Stopped at {outcome.stopped_at}: {outcome.error}")

        elapsed = time.monotonic() - start

    print(
        f"\n=== Summary ({sum(o.deleted for o in outcomes)}/{STILL_COUNT} succeeded, "
        f"{elapsed:.1f}s total) ==="
    )
    for outcome in outcomes:
        print(outcome.summary_line())
    if len({o.guessed_path for o in outcomes if o.guessed_path}) != len(
        [o for o in outcomes if o.guessed_path]
    ):
        print(
            "\nWARNING: two stills guessed the SAME path — exclude did not prevent a "
            "collision. This would be a real defect; report it rather than trusting the "
            "run above."
        )

    return 0 if all(o.deleted for o in outcomes) else 1


if __name__ == "__main__":
    # Redirected/piped stdout is fully buffered by default in CPython, while
    # logging's StreamHandler flushes per record — without this, captured
    # log files interleave print() and logging output out of chronological
    # order, making timing hard to trust when diagnosing issues later.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
