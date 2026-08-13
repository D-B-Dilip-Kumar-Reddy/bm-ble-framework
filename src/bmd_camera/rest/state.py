"""
bmd_camera/rest/state.py
===========================
CameraState — RestCameraSession's continuously-updated, notification-driven
state surface (Phase 9, closing design principle 4's *(planned)* status on
the REST side). Every field here is updated ONLY by `RestCameraSession._on_event`
from a real WS `propertyValueChanged` event — never inferred from a request
the session itself made. This is not new discipline: `is_recording`,
`last_known_storage`, `playback_interrupted`, `last_known_play`/
`last_known_stop`, and `_in_playback` all held this exact rule as plain
`RestCameraSession` attributes before this refactor (Phase 4, Phase 8 item 1,
Phase 8 item 2) — this file only gives them a home of their own instead of
letting them keep accreting directly on the session object. See
`docs/rest/session.md` for the real-hardware evidence behind each field,
carried forward unchanged by this move.

`RestCameraSession` exposes every field here through an identically-named
`@property` (e.g. `session.is_recording` reads/writes `session.state.
is_recording`) — external code, examples, and tools are unaffected by this
file existing at all.

SCOPE: REST only. The BLE side's `CameraSession` (`ble/session.py`) has its
own, separate, working notification-observer pattern (`is_recording`/
`last_stop_reason`/`last_known_codec_variant`/`last_known_recording_format`)
directly on plain attributes — not touched by this file, and `ble/state.py`
remains unbuilt and `*(planned)*` (design principle 4's heading tag stays,
since the principle as titled is transport-agnostic and BLE isn't done).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StorageDevice:
    """One member of `GET /media/workingset`'s fixed-size `workingset`
    array — including empty slots (`device_name == ""`). Never assume
    index 0 is the active device; the working set can (and on real
    hardware does) hold the active disk at a different index."""

    index: int
    device_name: str
    active: bool
    total_space: int
    remaining_space: int = 0
    remaining_record_time: int = 0
    clip_count: int = 0
    volume: str | None = None


@dataclass(frozen=True)
class StorageState:
    """Design principle 10's first real implementation — everything storage-
    aware operations need: card presence (`devices`), the active member
    (`active_device`, resolved from `GET /media/active` rather than by
    guessing an index), remaining space, remaining record time, clip count.
    `active_device` is `None` if no device in the working set reports
    itself active."""

    devices: tuple[StorageDevice, ...]
    active_device: StorageDevice | None


@dataclass
class CameraState:
    """The notification-driven fields `RestCameraSession` tracks
    continuously rather than fetching on demand. Not frozen — updated in
    place by `_on_event` as real events arrive, exactly as the equivalent
    plain attributes were before this refactor.

    `_in_playback` keeps its underscore despite living on a dataclass other
    code reaches into — it was never meant as public API on `RestCameraSession`
    either (only `tools/rest/verify_playback_interrupt.py`'s diagnostic
    tooling reaches past the underscore, the same tolerated pattern
    `diagnose_timeline.py` already uses for `_rest_client`). `session.
    _in_playback` continues to work unchanged via its own property.
    """

    is_recording: bool | None = None
    last_known_storage: StorageState | None = None
    _in_playback: bool = False
    last_known_play: bool | None = None
    last_known_stop: bool | None = None
    playback_interrupted: asyncio.Event = field(default_factory=asyncio.Event)
