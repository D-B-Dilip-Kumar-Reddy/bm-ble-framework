"""
bmd_camera/rest/media.py
===========================
Photo-capture confirmation over REST — the out-of-band verification channel
BLE's photo trigger has never had (`docs/ble/photo_capture.md` §7.3's open
TODO). No BLE knowledge lives here — design principle 5's boundary, held
here for REST.

WHY THIS EXISTS
-----------------
`CameraSession.capture_photo()` (BLE) sends the confirmed trigger
(category `0x0A`/parameter `0x03`/`VOID`) but cannot verify anything: no
BLE channel — echo or `CAMERA_STATUS` — has ever been observed to move in
response, on either camera tested (`docs/ble/photo_capture.md` §7, §9).
This module supplies the confirmation BLE structurally cannot: watch for a
new still file to appear on the SD card, over REST, after the BLE trigger
fires. See `examples/capture_photo.py` for the composition of both.

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

STILLS CANNOT BE LISTED
--------------------------
Every subdirectory under a mount root `500`s (`docs/rest/transport.md`,
"The 500 is not Stills-specific"), Stills included. So this module cannot
list a Stills directory's contents — it probes individual filenames
directly via `RestCameraSession.path_exists()`, exactly as
`docs/rest/transport.md`'s Phase 6 section anticipated.

FILENAME PATTERN — inherited from the original plan, not yet re-confirmed
by a sweep run in this codebase
-----------------------------------------------------------------------------
A clip's `filePath` basename (e.g. `A001_07311253_C001.mov`) and a still's
(e.g. `A001_07311253_S001.dng`) were reported sharing the `A001_07311253`
stem — operator-provided at planning time, not yet independently
re-confirmed by any tool in this codebase. `derive_still_prefix()` assumes
this pattern holds; treat its output as a candidate to verify on first
real use, not an established fact the way
`docs/ble/photo_capture.md`'s trigger finding is.

STILL FILE FORMAT DIFFERS BY CAMERA AND CODEC
--------------------------------------------------
`docs/ble/photo_capture.md` §8.4: `POCKET_6K_G2 v7.9` stills are always
`.dng`; `POCKET_6K_PRO v8.6` stills are `.dng` under ProRes but `.braw`
under BRAW. `find_highest_still_index()`/`wait_for_new_still()` probe both
extensions for this reason, never assuming one.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session import RestCameraSession

STILL_EXTENSIONS = (".dng", ".braw")

_CLIP_STEM_RE = re.compile(r"^(?P<prefix>.+)_C\d+$")


def derive_still_prefix(clip_file_path: str) -> str:
    """The filename stem a still is expected to share with a clip's own
    `filePath` — e.g. `/mnt/sd0/A001/A001_07311253_C001.mov` ->
    `A001_07311253`.

    Strips the directory, extension, and trailing `_C<digits>` clip-index
    suffix a real clip filename carries (`docs/rest/transport.md`'s sample:
    `A001_07311253_C001.mov`). Raises `ValueError` if the filename doesn't
    match this shape at all, rather than returning a wrong guess.
    """
    basename = clip_file_path.rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    match = _CLIP_STEM_RE.match(stem)
    if match is None:
        raise ValueError(
            f"Clip filename {basename!r} does not match the expected "
            "<prefix>_C<NNN>.<ext> shape (docs/rest/transport.md) — cannot "
            "derive a still filename prefix from it."
        )
    return match.group("prefix")


def resolve_mount_path(mount_names: tuple[str, ...], *, volume: str | None) -> str:
    """The `/mounts/<name>/` path to use for still-file probing, resolved
    from `GET /mounts/`'s own real directory listing — never by
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
    — the mount path `find_highest_still_index`/`wait_for_new_still` probe
    under."""
    storage = await session.storage_state()
    volume = storage.active_device.volume if storage.active_device else None
    names = await session.mount_names()
    return resolve_mount_path(names, volume=volume)


async def find_highest_still_index(
    session: RestCameraSession,
    mount_path: str,
    prefix: str,
    *,
    extensions: tuple[str, ...] = STILL_EXTENSIONS,
    max_index: int = 999,
) -> int | None:
    """The highest `_S<NNN>` still index currently on the card for `prefix`,
    or `None` if none exist yet.

    Probes `{mount_path}Stills/{prefix}_S{index:03d}{ext}` for `index` = 1,
    2, 3, ... — bounded by `max_index` — stopping at the first index where
    none of `extensions` exists via `session.path_exists()`. Call this
    *before* triggering `CameraSession.capture_photo()` to get a baseline;
    `wait_for_new_still()` is what confirms the trigger afterward.
    """
    highest: int | None = None
    index = 1
    while index <= max_index:
        found = False
        for ext in extensions:
            path = f"{mount_path}Stills/{prefix}_S{index:03d}{ext}"
            if await session.path_exists(path):
                found = True
                break
        if not found:
            break
        highest = index
        index += 1
    return highest


async def wait_for_new_still(
    session: RestCameraSession,
    mount_path: str,
    prefix: str,
    baseline_index: int | None,
    *,
    extensions: tuple[str, ...] = STILL_EXTENSIONS,
    timeout_s: float,
    poll_interval_s: float = 1.0,
) -> int | None:
    """Poll for a still at index `(baseline_index or 0) + 1` to appear,
    returning its index once found or `None` on timeout.

    This is the actual per-photo confirmation design principle 3 asks for
    — a specific new file appearing, not a coarse "some write happened"
    signal (the `0x09/0x02` BLE lead, `docs/ble/photo_capture.md` §5.3, is
    exactly that too-coarse signal — moves roughly once per three photos,
    not once per photo). Call `find_highest_still_index()` first, before
    triggering the BLE capture, to get `baseline_index`.
    """
    target_index = (baseline_index or 0) + 1
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for ext in extensions:
            path = f"{mount_path}Stills/{prefix}_S{target_index:03d}{ext}"
            if await session.path_exists(path):
                return target_index
        await asyncio.sleep(poll_interval_s)
    return None
