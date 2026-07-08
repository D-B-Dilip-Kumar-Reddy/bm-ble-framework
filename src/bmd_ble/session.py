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
"""

from __future__ import annotations

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


class CameraSession:
    """Async context manager: connect, verified operations, disconnect."""

    def __init__(self, model_key: str, firmware: str, *, echo_timeout_s: float = 2.0) -> None:
        self.profile = CameraProfile.for_model(model_key=model_key, firmware=firmware)
        self.echo_timeout_s = echo_timeout_s
        self._router = NotificationRouter()
        self._controller: BMDCameraController | None = None

    async def __aenter__(self) -> CameraSession:
        discovered = await scan_for_camera(self.profile.ble_name)
        self._controller = BMDCameraController(discovered=discovered, profile=self.profile)
        await self._controller.connect()
        # Buffer before any write is possible — see NotificationRouter's docstring.
        await self._controller.subscribe_incoming(callback=self._router.handle_incoming)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._controller is not None:
            await self._controller.disconnect()

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
