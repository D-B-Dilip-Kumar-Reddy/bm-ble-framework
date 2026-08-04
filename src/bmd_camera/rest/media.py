"""
bmd_camera/rest/media.py
===========================
Photo-capture confirmation over REST — the out-of-band verification channel
BLE's photo trigger has never had (`docs/ble/photo_capture.md` §7.3's open
TODO, §11's real-hardware correction). No BLE knowledge lives here — design
principle 5's boundary, held here for REST.

WHY THIS EXISTS
-----------------
`CameraSession.capture_photo()` (BLE) sends the confirmed trigger
(category `0x0A`/parameter `0x03`/`VOID`) but cannot verify anything: no
BLE channel — echo or `CAMERA_STATUS` — has ever been observed to move in
response, on either camera tested (`docs/ble/photo_capture.md` §7, §9).
This module supplies the confirmation BLE structurally cannot: watch the
Stills directory itself change on the SD card, over REST, after the BLE
trigger fires. See `examples/capture_photo.py` for the composition of both.

WHY THE MOUNT PATH ISN'T DERIVED FROM `deviceName`
------------------------------------------------------
A single real sample showed `/clips/list`'s `filePath`
(`/mnt/sd0/A001/...`) mapping to the HTTP mount `/mounts/A001-sd1/` —
`deviceName` `"sd0"` against mount suffix `"sd1"`
(`docs/rest/transport.md`, "The clip-path mapping — a pattern, not yet a
rule"). That doc is explicit this is one data point, not a confirmed rule
("not something to encode as a rule without a second reel or a second slot
to test it against") — so this module never performs that substitution.
Instead `resolve_active_mount()` reads `GET /mounts/`'s own real directory
listing (`RestCameraSession.mount_names()`, already confirmed to return
real entries) and resolves the mount from what the camera actually
reports: unambiguous when there is exactly one mount, or by matching the
confirmed `volume` prefix (design principle 1 — only using data the camera
itself already reported) when there is more than one.

WHY THIS ISN'T FILENAME PROBING — a real-hardware finding, 2026-08-04
--------------------------------------------------------------------------
The original design here assumed a still shares a clip's full
`<reel>_<date>` filename stem (an operator sample at planning time:
`A001_07311253_C001.mov` / `A001_07311253_S001.dng`). The first real
`capture_photo.py` run against `POCKET_6K_PRO v8.6` falsified that:
`mount_names()`/`wait_for_new_still()` ran cleanly but reported "NOT
confirmed", yet pulling the card afterward showed the photo really was
taken. Three real stills were on the card: `A001_07311253_S001`,
`A001_07311254_S002`, `A001_08041126_S003` — the middle timestamp segment
is unique **per still**, one second apart on two photos taken back to
back, and three days apart on the third. Only the leading reel identifier
(`A001`) is actually shared with clips; the timestamp segment is each
photo's own capture moment, generated fresh every time, and the trailing
`_S<NNN>` counter is a reel-wide cumulative count with no knowable
baseline (Stills directory listing `500`s unconditionally — see below —
so there is no way to learn the current count in advance). A still's exact
filename cannot be predicted or brute-forced from clip data at all — the
former `derive_still_prefix()`/`find_highest_still_index()`/
`wait_for_new_still()` index-probing design is retired for this reason
(see git history for the removed implementation, and
`docs/ble/photo_capture.md` §11 for the full evidentiary record).

STILLS CANNOT BE LISTED, BUT THE MOUNT ROOT CAN
----------------------------------------------------
Every subdirectory under a mount root `500`s (`docs/rest/transport.md`,
"The 500 is not Stills-specific"), Stills included — so this module can
never enumerate what's actually inside Stills, or learn a still's exact
name, over REST. But the mount **root** listing works, and it already
reports `Stills` as one of its own entries, complete with an `mtime`
(`{"name": "Stills", "type": "directory", "mtime": "..."}`) — standard
filesystem behaviour updates a directory's own `mtime` whenever a file is
added inside it, without ever needing to open that directory. That gives a
genuine, per-operation confirmation signal that needs no filename
knowledge at all: read `Stills`'s `mtime` before the trigger fires, then
poll for it to change afterward. `stills_marker()` reads it;
`wait_for_new_still()` polls for the change. This specific mechanism is
not yet independently confirmed on real hardware — it is what this module
moved to *after* the filename-probing design failed its first real run,
and its own first real run is still pending.

THE TRADE-OFF: no *guaranteed* filename, but an opt-in best-effort guess
-----------------------------------------------------------------------------
Confirmation itself (`wait_for_new_still()`) never learns *which* filename
a still got — REST has no way to learn that (the `500` above). But the
real filenames observed on a pulled card (see above) follow a knowable
shape: `<reel>_<MMDDHHMM>_S<NNN><ext>`, where the reel is already known
(the mount name's own prefix — `resolve_active_mount()` already resolves
it), the timestamp lands on the same minute the trigger was sent (real
evidence: a trigger logged at `11:26:24` produced a still stamped exactly
`08041126`), and only the counter `<NNN>` is genuinely unknowable without
a listing. `guess_new_still_path()` exploits this: it probes a caller-
supplied, deliberately narrow set of `(minute offset, index, extension)`
combinations via `RestCameraSession.path_exists()`, returning the first
match. It is **opt-in, informational only, and never gates
`wait_for_new_still()`'s own pass/fail** — a caller with no reasonable
index range to try should not call it at all rather than brute-forcing an
unbounded range (see its docstring for why an unbounded default would be
both slow and unreliable). `examples/capture_photo.py` calls it once,
after confirmation, purely to print a likely name.

STILL FILE FORMAT DIFFERS BY CAMERA AND CODEC
--------------------------------------------------
`docs/ble/photo_capture.md` §8.4: `POCKET_6K_G2 v7.9` stills are always
`.dng`; `POCKET_6K_PRO v8.6` stills are `.dng` under ProRes but `.braw`
under BRAW. Irrelevant to confirmation (the `mtime` signal doesn't care
about extension) but directly relevant to `guess_new_still_path()`, which
defaults to trying both extensions per candidate since it has no cheap way
to know the active codec on its own.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .session import RestCameraSession

STILLS_DIR_NAME = "Stills"
STILL_EXTENSIONS = (".dng", ".braw")


def resolve_mount_path(mount_names: tuple[str, ...], *, volume: str | None) -> str:
    """The `/mounts/<name>/` path to use for Stills-directory monitoring,
    resolved from `GET /mounts/`'s own real directory listing — never by
    transforming a `deviceName` into a guessed mount suffix (see module
    docstring).

    Unambiguous when exactly one mount exists. With more than one, narrows
    to entries starting with `f"{volume}-"` (the one pattern confirmed on
    real hardware — `docs/rest/transport.md`). Raises `ValueError` if that
    still leaves zero or more than one candidate — this function never
    guesses which mount to use.
    """
    if not mount_names:
        raise ValueError("GET /mounts/ reported no mounts — is a storage device inserted?")
    if len(mount_names) == 1:
        return f"/mounts/{mount_names[0]}/"
    if volume is None:
        raise ValueError(
            f"GET /mounts/ reports {len(mount_names)} mounts ({mount_names!r}) and no "
            "active volume is known to narrow them — cannot resolve which one to use."
        )
    candidates = [name for name in mount_names if name.startswith(f"{volume}-")]
    if len(candidates) != 1:
        raise ValueError(
            f"GET /mounts/ reports {len(mount_names)} mounts ({mount_names!r}); "
            f"{len(candidates)} start with {volume!r}-  — cannot resolve which one to use "
            "without a confirmed mapping rule (docs/rest/transport.md)."
        )
    return f"/mounts/{candidates[0]}/"


async def resolve_active_mount(session: RestCameraSession) -> str:
    """`storage_state()` (for the active device's `volume`) + `mount_names()`
    (the camera's own real mount listing), combined via `resolve_mount_path`
    — the mount path `stills_marker()`/`wait_for_new_still()` monitor."""
    storage = await session.storage_state()
    volume = storage.active_device.volume if storage.active_device else None
    names = await session.mount_names()
    return resolve_mount_path(names, volume=volume)


async def stills_marker(session: RestCameraSession, mount_path: str) -> str | None:
    """The Stills subdirectory's own `mtime`, from `mount_path`'s root
    listing — advances whenever a file is added to or removed from
    Stills, without ever needing to list Stills' own contents (which
    500s unconditionally — see module docstring). Returns `None` if no
    `Stills` entry exists yet (no photo has ever been taken on this
    card)."""
    entries = await session.list_mount(mount_path)
    for entry in entries:
        if entry.get("name") == STILLS_DIR_NAME and entry.get("type") == "directory":
            return entry.get("mtime")
    return None


async def wait_for_new_still(
    session: RestCameraSession,
    mount_path: str,
    baseline_marker: str | None,
    *,
    timeout_s: float,
    poll_interval_s: float = 0.5,
) -> bool:
    """Poll `mount_path`'s root listing until the Stills subdirectory's
    `mtime` differs from `baseline_marker` (or first appears, if
    `baseline_marker` is `None` — no Stills directory existed yet), or
    `timeout_s` elapses.

    This is the real per-operation confirmation design principle 3 asks
    for — the Stills directory's own filesystem-level change, never a
    filename guess (see module docstring for why filename-based
    *confirmation* was retired — `guess_new_still_path()` exists for an
    opt-in, purely informational name lookup, and never feeds back into
    this function) and not the coarse `0x09/0x02` BLE write-margin lead
    (`docs/ble/photo_capture.md` §5.3), which moves roughly once per three
    photos, not once per photo. Call `stills_marker()` first, before
    triggering the BLE capture, to get `baseline_marker`.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        marker = await stills_marker(session, mount_path)
        if marker is not None and marker != baseline_marker:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_interval_s)


def _reel_from_mount_path(mount_path: str) -> str:
    """The reel identifier a mount path's own name starts with — e.g.
    `/mounts/A001-sd1/` -> `"A001"`. Relies on the one pattern confirmed on
    real hardware, `"<reel>-<slot label>"` (`docs/rest/transport.md`'s "The
    clip-path mapping"), already leaned on by `resolve_mount_path()`'s own
    volume-prefix narrowing — this is the same assumption, not a new one."""
    mount_name = mount_path.removeprefix("/mounts/").rstrip("/")
    return mount_name.split("-", 1)[0]


async def guess_new_still_path(
    session: RestCameraSession,
    mount_path: str,
    *,
    around: datetime,
    index_candidates: Iterable[int] = range(1, 11),
    minute_offsets: tuple[int, ...] = (0, 1, -1),
    extensions: tuple[str, ...] = STILL_EXTENSIONS,
) -> str | None:
    """Best-effort reconstruction of a just-confirmed still's exact
    filename — opt-in, informational only, and never a substitute for
    `wait_for_new_still()`'s own confirmation (see module docstring).

    Real stills follow `<reel>_<MMDDHHMM>_S<NNN><ext>`, where the reel is
    already known (derived from `mount_path`'s own name — the camera's own
    reported mount, not guessed) and the timestamp lands on the same
    minute the trigger was sent (real evidence: a trigger logged at
    `11:26:24` produced a still stamped exactly `08041126`). Only the
    counter `<NNN>` is genuinely unknowable without a directory listing
    (Stills always `500`s — module docstring), so this probes every
    `(minute offset, index, extension)` combination in
    `index_candidates` × `minute_offsets` × `extensions`.

    Within a given `minute_offsets` entry, `index_candidates` is always
    checked **highest first**, regardless of the order passed in — a real
    hardware defect, 2026-08-04: two photos taken 30 seconds apart landed
    in the same clock-minute, so both matched the same timestamp candidate,
    and an earlier ascending-order search returned the *lower* (stale,
    already-existing) index both times instead of the just-written one.
    Since the counter only ever grows, the highest existing index that
    matches a given timestamp is always the most recently captured one —
    checking high-to-low fixes this without needing any new signal.
    `minute_offsets` itself is still checked in the order given (most
    likely timestamp first) and the first offset with any match wins, so
    this does not fully solve every ambiguous case (a genuinely wrong
    timestamp guess with its own stale match would still win over a correct
    but untried one) — only the specific, observed failure mode.

    `index_candidates` defaults to a narrow `range(1, 11)` deliberately —
    this function has no way to know how many stills already exist on the
    card (the same limitation that retired filename-based *confirmation*),
    so an unbounded search would be both slow (one HTTP request per
    combination) and likely to still come back empty on a heavily-used
    card. Callers who have an actual hint (e.g. a previously confirmed
    index) should pass a narrow range built around it, such as
    `range(hint - 2, hint + 3)`, for a fast, well-targeted probe instead of
    this default shot in the dark.

    Returns `None` if nothing in the search space matches — this is not
    evidence the capture failed (`wait_for_new_still()` already settled
    that); it only means this function's guess was wrong or the true
    index fell outside `index_candidates`.
    """
    reel = _reel_from_mount_path(mount_path)
    for offset in minute_offsets:
        stamp = (around + timedelta(minutes=offset)).strftime("%m%d%H%M")
        for index in sorted(index_candidates, reverse=True):
            for ext in extensions:
                path = f"{mount_path}{STILLS_DIR_NAME}/{reel}_{stamp}_S{index:03d}{ext}"
                if await session.path_exists(path):
                    return path
    return None
