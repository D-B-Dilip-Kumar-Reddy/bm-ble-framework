"""
bmd_ble/session.py
====================
CameraSession — the only surface user scripts touch (CLAUDE.md design
principle 5). Composes CameraController (BLE transport) and NotificationRouter
(echo buffering) to provide verified camera operations.

STATUS: minimal. Only record start/stop are implemented, with echo-only
verification (see docs/session_and_verification.md for why CAMERA_STATUS
can't yet serve as the secondary cross-check CLAUDE.md's verification
strategy calls for). Storage preconditions, GAP/device metadata, and
reconnect wiring beyond what CameraController already provides are not
implemented here yet.

Also tracks the latest TIMECODE reading and snapshots it around
record_start()/record_stop() so callers can read `last_clip_duration_seconds()`
— see docs/timecode.md for why duration is hours/minutes/seconds-only today.
"""

from __future__ import annotations

import contextlib
from typing import Any

from .camera_controller import BMDCameraController
from .camera_profile import CameraProfile
from .exceptions import BMDVerificationError
from .notification_router import NotificationRouter
from .protocol.categories.recording import (
    decode_recording_state,
    encode_record_start,
    encode_record_stop,
)
from .scanner import scan_for_camera
from .timecode import Timecode, decode_timecode, duration_seconds


class CameraSession:
    """Async context manager: connect, verified operations, disconnect."""

    def __init__(self, model_key: str, firmware: str, *, echo_timeout_s: float = 3.0) -> None:
        self.profile = CameraProfile.for_model(model_key=model_key, firmware=firmware)
        self.echo_timeout_s = echo_timeout_s
        self._router = NotificationRouter()
        self._controller: BMDCameraController | None = None
        self._latest_timecode: Timecode | None = None
        self.last_start_timecode: Timecode | None = None
        self.last_stop_timecode: Timecode | None = None

    async def __aenter__(self) -> CameraSession:
        discovered = await scan_for_camera(self.profile.ble_name)
        self._controller = BMDCameraController(discovered=discovered, profile=self.profile)
        await self._controller.connect()
        # Buffer before any write is possible — see NotificationRouter's docstring.
        await self._controller.subscribe_incoming(callback=self._router.handle_incoming)
        await self._controller.subscribe_timecode(callback=self._handle_timecode)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._controller is not None:
            await self._controller.disconnect()

    def _handle_timecode(self, _characteristic: Any, data: bytearray) -> None:
        """TIMECODE callback — decodes and stores the latest reading. Never raises."""
        with contextlib.suppress(ValueError):
            self._latest_timecode = decode_timecode(bytes(data))

    def last_clip_duration_seconds(self) -> float | None:
        """Elapsed seconds between the last confirmed record_start and record_stop.

        None if either snapshot is missing (e.g. no TIMECODE notification had
        arrived yet at that moment) or if record_stop's timecode isn't after
        record_start's (see timecode.duration_seconds).
        """
        if self.last_start_timecode is None or self.last_stop_timecode is None:
            return None
        try:
            return duration_seconds(self.last_start_timecode, self.last_stop_timecode)
        except ValueError:
            return None

    async def record_start(self) -> None:
        """Start recording, raising BMDVerificationError unless confirmed."""
        await self._set_recording_state(recording=True)

    async def record_stop(self) -> None:
        """Stop recording, raising BMDVerificationError unless confirmed."""
        await self._set_recording_state(recording=False)

    async def _set_recording_state(self, *, recording: bool) -> None:
        spec = self.profile.require_command("recording", ("start", "stop"))
        category = spec.category
        parameter = spec.parameter
        value = spec.values["start" if recording else "stop"]
        encode = encode_record_start if recording else encode_record_stop
        command = encode(
            category=category,
            parameter=parameter,
            data_type=spec.data_type,
            value=value,
            reserved=spec.reserved,
        )

        action = "record_start" if recording else "record_stop"

        self._router.arm(category, parameter)
        await self._controller.write_outgoing_control(command)
        result = await self._router.wait_for(category, parameter, timeout=self.echo_timeout_s)

        if result is None:
            raise BMDVerificationError(f"{action}: no echo received within {self.echo_timeout_s}s")

        _header, payload = result
        confirmed = decode_recording_state(payload, spec.data_type)
        if confirmed != recording:
            raise BMDVerificationError(
                f"{action}: echo confirmed recording={confirmed}, expected {recording}"
            )

        if recording:
            self.last_start_timecode = self._latest_timecode
        else:
            self.last_stop_timecode = self._latest_timecode
