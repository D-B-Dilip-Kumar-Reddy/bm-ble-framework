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

Also tracks the latest TIMECODE reading. A confirmed record_start() snapshots
a canonical 00:00:00:00 (TIMECODE is known to reset at recording start on
real Blackmagic hardware — see docs/timecode.md); a confirmed record_stop()
snapshots the latest TIMECODE reading seen. Callers read the elapsed time via
`last_clip_duration_seconds()` — see docs/timecode.md for why duration is
hours/minutes/seconds-only today.

After connecting, __aenter__ waits `connect_settle_s` before returning — a
just-connected camera floods the link with an initial info dump (lens,
media, status), and a command sent immediately can queue behind it and take
several seconds to echo, well past a normal command's echo_timeout_s. See
docs/session_and_verification.md.

Also watches every INCOMING_CONTROL notification (not just the ones a
pending record_start()/record_stop() call is awaiting) for the recording
category, so `is_recording` reflects the camera's actual last-reported state
and a camera-initiated stop — observed on real hardware when the SD card's
write speed can't keep up — is detected immediately instead of only being
noticed the next time record_stop() is called. See docs/recording.md.

It also watches for a second, CANDIDATE-status signal — a storage
notification observed to precede that same camera-initiated stop on real
hardware — and surfaces it as `last_stop_signal` when it fires shortly
before an unexpected stop. This is a narrow diagnostic annotation, not the
CLAUDE.md-planned storage-monitoring subsystem (no card-ready checks, no
capacity tracking) — see docs/recording.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from .camera_controller import BMDCameraController
from .camera_profile import CameraProfile
from .exceptions import BMDVerificationError
from .notification_router import NotificationRouter
from .protocol.categories.recording import (
    decode_recording_state,
    encode_record_start,
    encode_record_stop,
    is_recording_state_echo,
)
from .protocol.categories.storage import decode_write_margin, is_storage_notification
from .protocol.codec import decode_packet
from .scanner import scan_for_camera
from .timecode import Timecode, decode_timecode, duration_seconds


class CameraSession:
    """Async context manager: connect, verified operations, disconnect."""

    def __init__(
        self,
        model_key: str,
        firmware: str,
        *,
        echo_timeout_s: float = 3.0,
        connect_settle_s: float = 6.0,
        write_margin_window_s: float = 2.0,
    ) -> None:
        self.profile = CameraProfile.for_model(model_key=model_key, firmware=firmware)
        self.echo_timeout_s = echo_timeout_s
        self.connect_settle_s = connect_settle_s
        # Real-hardware evidence: a low-write-margin warning has been
        # observed 0.1-1.4s before every camera-initiated stop seen so far
        # (6/6 occurrences, 2 camera models) — see docs/recording.md.
        self.write_margin_window_s = write_margin_window_s
        self._router = NotificationRouter()
        self._controller: BMDCameraController | None = None
        self._latest_timecode: Timecode | None = None
        self.last_start_timecode: Timecode | None = None
        self.last_stop_timecode: Timecode | None = None

        # Notification-derived recording state — never set from "we sent a
        # command", only from a decoded INCOMING_CONTROL recording-category
        # report (CLAUDE.md design principle 4). None until the first such
        # report arrives (e.g. before any record_start()/record_stop() call,
        # or on a profile without a recording command block).
        self.is_recording: bool | None = None
        # "requested" after a caller-initiated record_stop() confirms;
        # "unexpected" after a stop the caller never asked for is observed
        # (e.g. real-hardware evidence: the camera auto-stops recording when
        # the SD card's write speed can't keep up — see docs/recording.md).
        # The *specific reason* for an unexpected stop isn't decodable from
        # the wire yet — only that one happened.
        self.last_stop_reason: str | None = None
        # A separate, additive attribute (not folded into last_stop_reason,
        # which stays exactly "requested" | "unexpected" | None) — set to
        # "low_write_margin" when a CANDIDATE low-margin storage signal was
        # observed shortly before an unexpected stop. None otherwise,
        # including for every requested stop. See docs/recording.md.
        self.last_stop_signal: str | None = None
        self._last_low_margin_at: float | None = None
        self._unexpected_stop_event = asyncio.Event()
        self._pending_command = False

    async def __aenter__(self) -> CameraSession:
        discovered = await scan_for_camera(self.profile.ble_name)
        self._controller = BMDCameraController(discovered=discovered, profile=self.profile)
        await self._controller.connect()
        # Buffer before any write is possible — see NotificationRouter's docstring.
        await self._controller.subscribe_incoming(callback=self._handle_incoming)
        await self._controller.subscribe_timecode(callback=self._handle_timecode)
        # Let the just-connected camera's initial info dump settle before any
        # command can be sent — see module docstring and
        # docs/session_and_verification.md for the real-hardware evidence.
        await asyncio.sleep(self.connect_settle_s)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._controller is not None:
            await self._controller.disconnect()

    def _handle_incoming(self, characteristic: Any, data: bytearray) -> None:
        """INCOMING_CONTROL callback: feeds the echo router, then watches for
        an unsolicited recording-state change and the CANDIDATE write-margin
        signal. Never raises."""
        self._router.handle_incoming(characteristic, data)
        self._observe_recording_state(data)
        self._observe_write_margin(data)

    def _observe_recording_state(self, data: bytearray) -> None:
        """Update `is_recording` from any recording-category notification,
        and flag an unexpected stop — one this session isn't currently
        awaiting its own record_stop() echo for. Never raises: a profile
        without a recording block, or a packet this can't decode, is simply
        not a recording-state report.
        """
        try:
            spec = self.profile.require_command("recording", ("start", "stop"))
        except ValueError:
            return
        try:
            header, payload = decode_packet(bytes(data))
        except ValueError:
            return
        if not is_recording_state_echo(header, category=spec.category, parameter=spec.parameter):
            return
        try:
            confirmed = decode_recording_state(payload, spec.data_type)
        except ValueError:
            return

        was_recording = self.is_recording
        self.is_recording = confirmed
        if confirmed and not was_recording:
            # A new clip started — a stale low-margin reading from the
            # *previous* clip must not leak into this one's stop classification.
            self._last_low_margin_at = None
        if was_recording and not confirmed and not self._pending_command:
            # Mirrors the requested-stop branch in _set_recording_state, so
            # last_clip_duration_seconds() stays meaningful whether the stop
            # was requested or observed unsolicited.
            self.last_stop_timecode = self._latest_timecode
            self.last_stop_reason = "unexpected"
            if (
                self._last_low_margin_at is not None
                and time.monotonic() - self._last_low_margin_at <= self.write_margin_window_s
            ):
                self.last_stop_signal = "low_write_margin"
            self._unexpected_stop_event.set()

    def _observe_write_margin(self, data: bytearray) -> None:
        """Track the most recent CANDIDATE low-write-margin storage signal
        (see docs/recording.md's "Camera-initiated stop detection" section).
        Never raises: a profile without the storage block, or a packet this
        can't decode, simply isn't this signal.
        """
        try:
            spec = self.profile.require_storage_signal(
                "write_margin_warning", ("nominal", "low_margin")
            )
        except ValueError:
            return
        try:
            header, payload = decode_packet(bytes(data))
        except ValueError:
            return
        if not is_storage_notification(header, category=spec.category, parameter=spec.parameter):
            return
        try:
            value = decode_write_margin(payload, spec.data_type, byte_offset=spec.byte_offset)
        except ValueError:
            return

        if value == spec.values["low_margin"]:
            self._last_low_margin_at = time.monotonic()

    def _handle_timecode(self, _characteristic: Any, data: bytearray) -> None:
        """TIMECODE callback — decodes and stores the latest reading. Never raises."""
        with contextlib.suppress(ValueError):
            self._latest_timecode = decode_timecode(bytes(data))

    async def wait_while_recording(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds, returning early if recording stops
        unexpectedly (e.g. the camera auto-stopping on a slow SD card).

        Returns True if still recording when `timeout` elapses, False if an
        unexpected stop was observed before then — callers should check this
        instead of blindly `asyncio.sleep`ing for a planned recording
        duration, so a script can move on immediately rather than waiting
        out the rest of a recording that already ended. See docs/recording.md.
        """
        self._unexpected_stop_event.clear()
        try:
            await asyncio.wait_for(self._unexpected_stop_event.wait(), timeout=timeout)
        except TimeoutError:
            return True
        return False

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
        """Stop recording, raising BMDVerificationError unless confirmed.

        A no-op if `is_recording` already positively confirms the camera
        isn't recording (e.g. `wait_while_recording()` already reported an
        unexpected stop). Sending a redundant stop in that state has been
        observed on real hardware to simply never echo — the camera was
        already in the requested state, so raising BMDVerificationError for
        a timeout there would be misleading, not a real verification
        failure. See docs/recording.md.
        """
        if self.is_recording is False:
            return
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

        # While a command we issued ourselves is in flight, its own echo
        # (however it decodes) must never be mistaken for an *unexpected*
        # stop by _observe_recording_state — see that method's docstring.
        self._pending_command = True
        try:
            self._router.arm(category, parameter)
            await self._controller.write_outgoing_control(command)
            result = await self._router.wait_for(category, parameter, timeout=self.echo_timeout_s)

            if result is None:
                raise BMDVerificationError(
                    f"{action}: no echo received within {self.echo_timeout_s}s"
                )

            _header, payload = result
            confirmed = decode_recording_state(payload, spec.data_type)
            if confirmed != recording:
                raise BMDVerificationError(
                    f"{action}: echo confirmed recording={confirmed}, expected {recording}"
                )
        finally:
            self._pending_command = False

        if recording:
            # Confirmed on real hardware (POCKET_6K_G2 v7.9 and POCKET_6K_PRO
            # v8.6): TIMECODE resets to 00:00:00:00 the moment recording
            # starts — a documented behavior across Blackmagic cameras, not
            # specific to these two. Snapshotting `_latest_timecode` here
            # instead would often grab a STALE reading left over from the
            # *previous* clip's end (no new TIMECODE notification necessarily
            # arrives between the previous stop and this start's echo), which
            # silently produced wrong/negative clip durations. See
            # docs/timecode.md.
            self.last_start_timecode = Timecode(hours=0, minutes=0, seconds=0, frames=0)
        else:
            self.last_stop_timecode = self._latest_timecode
            self.last_stop_reason = "requested"
