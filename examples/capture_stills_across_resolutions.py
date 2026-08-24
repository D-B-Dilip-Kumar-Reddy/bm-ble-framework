"""
Sweep the camera through several (codec, quality variant, resolution, fps)
combinations via `RestCameraSession.set_camera_format`, capturing
`STILLS_PER_FORMAT` real stills at each one — guessed, downloaded, and
deleted, reusing `examples/capture_multiple_stills.py`'s exact per-still
sequence (held-open BLE connection, `exclude`, adaptive index search,
`INTER_STILL_DELAY_S`, and the size-stability download check).

WHY THIS SCRIPT EXISTS
-----------------------
`capture_multiple_stills.py`'s download-completion check (download, wait,
download again, accept once two consecutive reads agree on a nonzero size)
replaced an earlier fixed-byte-count floor specifically because that floor
was tuned from a single data point — `.braw` stills at one resolution, one
camera (`docs/rest/session.md`'s `capture_multiple_stills.py` section). The
stability check was designed to generalize across codec/resolution without
retuning a constant, but that generalization claim itself was unconfirmed —
every real-hardware run to date used whatever format the camera already
happened to be in. This script is the real-hardware test of that claim: it
deliberately switches format between captures, spanning both a codec change
(BRAW vs ProRes) and a resolution change (6K vs HD) — the two axes
`docs/ble/photo_capture.md` §8/§8.4 identifies as the ones that actually
move still file size — and reports the observed still size at each format
in its summary, so a real run either confirms the sizes vary as expected
(and the stability check still lands on the right one each time) or
surfaces a combination the check doesn't handle.

`FORMATS` DEFAULT CHOICES
---------------------------
- `("BRAW", "3:1", "6K", FPS)` — the largest resolution this profile
  offers short of the anamorphic/17:9 variants, expected to produce the
  largest stills.
- `("BRAW", "3:1", "HD", FPS)` — same codec, smallest resolution, expected
  to produce the smallest BRAW stills — isolates resolution's effect on
  size with codec held constant.
- `("ProRes", "422", "4K DCI", FPS)` — a codec change at a third
  resolution. Also, deliberately, the exact combination
  `docs/ble/settings.md` records as `known_unreachable` over **BLE** on
  this profile (nine falsification attempts, all silent) but real-hardware-
  confirmed reachable over **REST** (`docs/rest/session.md`,
  `examples/rest_change_format.py`'s own reasoning) — `set_camera_format`
  here is a `RestCameraSession` method, so the BLE-only restriction never
  applies to this script at all.

Edit `FORMATS`/`STILLS_PER_FORMAT` to sweep a different set — every
resolution/codec name must appear in the profile's own `resolutions`/
`codecs` tables (`payloads/models/<MODEL_KEY>/ble/<FIRMWARE>.json`), and
`set_camera_format` checks the combination against the camera's own live
`GET /system/supportedFormats` before writing anything, raising
`BMDUnsupportedError` immediately if the camera doesn't report offering it
— see `set_camera_format`'s own docstring.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: codec family, quality variant,
resolution, and frame rate — once per entry in `FORMATS`, in order. The
camera is left in the *last* format from `FORMATS` when the script exits,
not restored to whatever it was set to beforehand — note your camera's
current settings before running (same caveat `rest_change_format.py`
gives). Also takes `STILLS_PER_FORMAT` real photos per format, each
downloaded to `DEST_DIR` and then deleted from the card — net effect on
the card is nothing once a still's round trip succeeds, same as
`capture_multiple_stills.py`.

PARTIAL FAILURE IS EXPECTED, NOT FATAL — at two levels. If a format switch
itself fails (`BMDVerificationError`/`BMDUnsupportedError`/`ValueError`),
that format's stills are skipped entirely and the sweep continues with the
next format, the same way `rest_change_format.py` reports a failed step
without aborting. Within a format, one still's failure at any step does
not stop that format's remaining stills, or later formats — the same
partial-success philosophy `capture_multiple_stills.py`/`delete_clips()`/
`download_clips()` already established.

STATUS: every mechanism this script reuses from `capture_multiple_stills.py`
(the held-open connection, `exclude`, the adaptive index search,
`INTER_STILL_DELAY_S`, and the stability-based download check) is
real-hardware-confirmed *within a single format* — this script's own
real-hardware run, which additionally exercises `set_camera_format`
mid-run and captures across genuinely different resolutions/codecs in one
session, is what's new and unconfirmed here.

Usage:
    python examples/capture_stills_across_resolutions.py
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

FPS = "23.98"
FORMATS = [
    ("BRAW", "3:1", "6K", FPS),
    ("BRAW", "3:1", "HD", FPS),
    ("ProRes", "422", "4K DCI", FPS),
]
STILLS_PER_FORMAT = 3

CONFIRM_TIMEOUT_S = 15.0
DEST_DIR = Path(__file__).parent / "downloads"

# Same rationale as capture_multiple_stills.py: the first still's index is
# unknown, later ones only increment. See that script's own comments.
INITIAL_INDEX_CANDIDATES = range(1, 51)
HINTED_INDEX_WINDOW = 5

# Real-hardware-confirmed minimum gap between physical captures
# (capture_multiple_stills.py's second real-hardware run).
INTER_STILL_DELAY_S = 3.0
# Settle time after a confirmed format switch, before the first still of
# that format is triggered — not itself real-hardware-confirmed necessary,
# a deliberate caution given a format switch is a much bigger camera-side
# change than a still trigger.
PAUSE_AFTER_FORMAT_CHANGE_S = 3.0

# Size-stability download check (capture_multiple_stills.py's fourth
# real-hardware run and its follow-up design change) — download, wait,
# download again, accept once two consecutive reads agree on a nonzero
# size. See that script's own module docstring for the full history of why
# this replaced a fixed byte-count floor.
STABILITY_CHECK_DELAY_S = 1.0
STABILITY_MAX_ATTEMPTS = 5


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
    format_label: str
    index: int
    captured: bool = False
    guessed_path: str | None = None
    downloaded_to: Path | None = None
    size: int | None = None
    deleted: bool = False
    stopped_at: str | None = None
    error: str = ""

    def summary_line(self) -> str:
        tag = f"[{self.format_label} #{self.index}]"
        if self.deleted:
            size_str = f" ({_format_bytes(self.size)})" if self.size is not None else ""
            return f"  {tag} OK — {self.guessed_path}{size_str}"
        stage = self.stopped_at or "unknown"
        return f"  {tag} FAILED at {stage} — {self.error}"


async def capture_one_still(
    ble_session: CameraSession,
    rest_session: RestCameraSession,
    mount_path: str,
    format_label: str,
    index: int,
    guessed_paths: list[str],
    last_confirmed_index: int | None,
) -> StillOutcome:
    outcome = StillOutcome(format_label=format_label, index=index)

    baseline = await stills_marker(rest_session, mount_path)

    trigger_time = datetime.now()
    try:
        await ble_session.capture_photo()
    except BMDUnsupportedError as exc:
        outcome.stopped_at = "trigger"
        outcome.error = str(exc)
        return outcome
    print(f"  [{format_label} #{index}] Trigger sent over BLE — confirming over REST …")

    confirmed = await wait_for_new_still(
        rest_session, mount_path, baseline, timeout_s=CONFIRM_TIMEOUT_S
    )
    if not confirmed:
        outcome.stopped_at = "confirm"
        outcome.error = f"Stills directory did not change within {CONFIRM_TIMEOUT_S}s"
        return outcome
    outcome.captured = True
    print(f"  [{format_label} #{index}] Confirmed ✓")

    index_candidates = _index_candidates_for(last_confirmed_index)
    guessed_path = await guess_new_still_path(
        rest_session,
        mount_path,
        around=trigger_time,
        index_candidates=index_candidates,
        exclude=guessed_paths,
    )
    if guessed_path is None and last_confirmed_index is not None:
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
    print(f"  [{format_label} #{index}] Guessed: {guessed_path}")

    dest: Path | None = None
    sizes: list[int] = []
    for attempt in range(1, STABILITY_MAX_ATTEMPTS + 1):
        try:
            dest = await rest_session.download_still(guessed_path, DEST_DIR, overwrite=True)
        except (ValueError, FileExistsError, BMDVerificationError) as exc:
            outcome.stopped_at = "download"
            outcome.error = str(exc)
            return outcome
        sizes.append(dest.stat().st_size)
        if sizes[-1] > 0 and len(sizes) >= 2 and sizes[-1] == sizes[-2]:
            break
        print(
            f"  [{format_label} #{index}] Download size not yet stable ({sizes[-1]} bytes) on "
            f"attempt {attempt}/{STABILITY_MAX_ATTEMPTS} — camera may still be writing the "
            f"file, retrying …"
        )
        if attempt < STABILITY_MAX_ATTEMPTS:
            await asyncio.sleep(STABILITY_CHECK_DELAY_S)
    else:
        outcome.stopped_at = "download"
        outcome.error = (
            f"file size never stabilized after {STABILITY_MAX_ATTEMPTS} attempts "
            f"(sizes observed: {sizes})"
        )
        return outcome
    size = sizes[-1]
    outcome.downloaded_to = dest
    outcome.size = size
    print(f"  [{format_label} #{index}] Downloaded: {dest} ({_format_bytes(size)})")

    try:
        await rest_session.delete_still(guessed_path, confirm=True)
    except (ValueError, BMDVerificationError) as exc:
        outcome.stopped_at = "delete"
        outcome.error = str(exc)
        return outcome
    outcome.deleted = True
    print(f"  [{format_label} #{index}] Deleted ✓")

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
        format_results: list[tuple[str, bool]] = []
        last_confirmed_index: int | None = None
        start = time.monotonic()

        for f_index, (codec, variant, resolution, fps) in enumerate(FORMATS, 1):
            label = f"{codec} {variant} {resolution} @ {fps}"
            print(f"\n=== Format {f_index}/{len(FORMATS)}: {label} ===")
            try:
                await rest_session.set_camera_format(codec, variant, resolution, fps)
            except (BMDVerificationError, BMDUnsupportedError, ValueError) as exc:
                print(f"  Format switch NOT confirmed: {exc} — skipping this format's stills")
                format_results.append((label, False))
                continue
            print("  Format confirmed ✓")
            format_results.append((label, True))
            print(f"  Waiting {PAUSE_AFTER_FORMAT_CHANGE_S}s for the camera to settle …")
            await asyncio.sleep(PAUSE_AFTER_FORMAT_CHANGE_S)

            for s_index in range(1, STILLS_PER_FORMAT + 1):
                if s_index > 1:
                    print(f"\n  Waiting {INTER_STILL_DELAY_S}s for the camera to settle …")
                    await asyncio.sleep(INTER_STILL_DELAY_S)
                print(f"\n  --- Still {s_index}/{STILLS_PER_FORMAT} ({label}) ---")
                outcome = await capture_one_still(
                    ble_session,
                    rest_session,
                    mount_path,
                    label,
                    s_index,
                    guessed_paths,
                    last_confirmed_index,
                )
                outcomes.append(outcome)
                if outcome.guessed_path is not None:
                    found_index = _index_from_path(outcome.guessed_path)
                    if found_index is not None:
                        last_confirmed_index = found_index
                if not outcome.deleted:
                    print(
                        f"  [{label} #{s_index}] Stopped at {outcome.stopped_at}: {outcome.error}"
                    )

        elapsed = time.monotonic() - start

    formats_ok = sum(ok for _, ok in format_results)
    stills_ok = sum(o.deleted for o in outcomes)
    print(
        f"\n=== Summary ({stills_ok}/{len(outcomes)} stills succeeded across "
        f"{formats_ok}/{len(FORMATS)} formats, {elapsed:.1f}s total) ==="
    )
    for label, ok in format_results:
        print(f"  Format {'OK    ' if ok else 'FAILED'}  {label}")
    print()
    for outcome in outcomes:
        print(outcome.summary_line())

    sizes_by_format: dict[str, set[int]] = {}
    for outcome in outcomes:
        if outcome.deleted and outcome.size is not None:
            sizes_by_format.setdefault(outcome.format_label, set()).add(outcome.size)
    if sizes_by_format:
        print("\n=== Observed still sizes by format ===")
        for label, sizes in sizes_by_format.items():
            print(f"  {label}: {sorted(sizes)}")

    if len({o.guessed_path for o in outcomes if o.guessed_path}) != len(
        [o for o in outcomes if o.guessed_path]
    ):
        print(
            "\nWARNING: two stills guessed the SAME path — exclude did not prevent a "
            "collision. This would be a real defect; report it rather than trusting the "
            "run above."
        )

    if not outcomes:
        return 1
    all_formats_ok = all(ok for _, ok in format_results)
    return 0 if all_formats_ok and all(o.deleted for o in outcomes) else 1


if __name__ == "__main__":
    # Redirected/piped stdout is fully buffered by default in CPython, while
    # logging's StreamHandler flushes per record — without this, captured
    # log files interleave print() and logging output out of chronological
    # order, making timing hard to trust when diagnosing issues later.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
