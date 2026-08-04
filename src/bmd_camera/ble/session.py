"""
bmd_camera/ble/session.py
====================
CameraSession — the only surface user scripts touch (CLAUDE.md design
principle 5). Composes CameraController (BLE transport) and NotificationRouter
(echo buffering) to provide verified camera operations.

STATUS: record start/stop are implemented with echo-only verification (see
docs/ble/session_and_verification.md for why CAMERA_STATUS can't yet serve as
the secondary cross-check CLAUDE.md's verification strategy calls for).
Settings writes (set_codec_quality / set_video_format /
set_recording_format) are implemented against the POCKET_6K_G2 v7.9
packet families in docs/ble/settings.md — all three are VERIFIED on real
hardware, including each family's no-echo-on-redundant-write behavior
(docs/ble/settings.md §11, §14), which every one now guards against itself.
set_camera_format
orchestrates all three from one (codec, variant, resolution, fps)
combination, including the two-step workaround the one known
dimension_enum gap (4K DCI/ProRes) needs — see its own docstring and
docs/ble/settings.md §9. Storage preconditions, GAP/device metadata, and
reconnect wiring beyond what CameraController already provides are not
implemented here yet.

Also tracks the latest TIMECODE reading. A confirmed record_start() snapshots
a canonical 00:00:00:00 (TIMECODE is known to reset at recording start on
real Blackmagic hardware — see docs/ble/timecode.md); a confirmed record_stop()
snapshots the latest TIMECODE reading seen. Callers read the elapsed time via
`last_clip_duration_seconds()` — see docs/ble/timecode.md for why duration is
hours/minutes/seconds-only today.

After connecting, __aenter__ waits `connect_settle_s` before returning — a
just-connected camera floods the link with an initial info dump (lens,
media, status), and a command sent immediately can queue behind it and take
several seconds to echo, well past a normal command's echo_timeout_s. See
docs/ble/session_and_verification.md.

Also watches every INCOMING_CONTROL notification (not just the ones a
pending record_start()/record_stop() call is awaiting) for the recording
category, so `is_recording` reflects the camera's actual last-reported state
and a camera-initiated stop — observed on real hardware when the SD card's
write speed can't keep up — is detected immediately instead of only being
noticed the next time record_stop() is called. See docs/ble/recording.md.

It also watches for a second, CANDIDATE-status signal — a storage
notification observed to precede that same camera-initiated stop on real
hardware — and surfaces it as `last_stop_signal` when it fires shortly
before an unexpected stop. This is a narrow diagnostic annotation, not the
CLAUDE.md-planned storage-monitoring subsystem (no card-ready checks, no
capacity tracking) — see docs/ble/recording.md.

Similarly watches every INCOMING_CONTROL notification for a codec_quality
report and remembers the last-seen (codec_id, variant_id) as
`last_known_codec_variant` — notification-derived only, same discipline as
`is_recording`. `set_codec_quality` uses it to skip a write that's already
known to be a no-op (real-hardware evidence, docs/ble/settings.md §11: a
`video_format` switch resets the new family's quality to a per-family
remembered value, and a caller-requested `set_codec_quality` for that same
value never echoes — indistinguishable from a real failure without this
check). Mirrors `record_stop()`'s `is_recording is False` early return.

The same silent-no-op behavior was subsequently confirmed on real hardware
(docs/ble/settings.md §14) for `video_format` and `recording_format` too —
sending either family a value it's already at produces no echo on any of
their watched channels. `last_known_recording_format` (a notification-
derived `(fps_int, width, height)`, updated from any recording_format-
category report — including `set_video_format`'s own mode-notify
confirmations, since they share the exact same (category, parameter))
gives `set_recording_format` the same guard `set_codec_quality` already
has. `set_video_format` reuses that field plus `last_known_codec_variant`
together — a redundant video_format write is one where the active codec
family *and* the active resolution+fps both already match the request.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from ..camera_profile import CameraProfile
from ..exceptions import BMDUnsupportedError, BMDVerificationError
from .camera_controller import BMDCameraController
from .notification_router import NotificationRouter
from .protocol.categories.media import encode_photo_trigger
from .protocol.categories.recording import (
    decode_recording_state,
    encode_record_start,
    encode_record_stop,
    is_recording_state_echo,
)
from .protocol.categories.settings import (
    decode_codec_quality,
    decode_recording_format,
    decode_video_format,
    encode_codec_quality,
    encode_recording_format,
    encode_video_format,
    is_settings_notification,
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
        # (6/6 occurrences, 2 camera models) — see docs/ble/recording.md.
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
        # the SD card's write speed can't keep up — see docs/ble/recording.md).
        # The *specific reason* for an unexpected stop isn't decodable from
        # the wire yet — only that one happened.
        self.last_stop_reason: str | None = None
        # A separate, additive attribute (not folded into last_stop_reason,
        # which stays exactly "requested" | "unexpected" | None) — set to
        # "low_write_margin" when a CANDIDATE low-margin storage signal was
        # observed shortly before an unexpected stop. None otherwise,
        # including for every requested stop. See docs/ble/recording.md.
        self.last_stop_signal: str | None = None
        self._last_low_margin_at: float | None = None
        self._unexpected_stop_event = asyncio.Event()
        self._pending_command = False
        # Notification-derived only (design principle 4), like is_recording
        # above — never set from "we sent a command". Updated from ANY
        # decoded codec_quality-category report, whether it followed our own
        # write, rode along on a video_format switch, or was body-initiated.
        # None until the first such report arrives. See set_codec_quality's
        # docstring for why this exists.
        self.last_known_codec_variant: tuple[int, int] | None = None
        # Same discipline, for the recording_format family: (fps_int, width,
        # height) from any recording_format-category report — including
        # set_video_format's own mode-notify confirmations, which share this
        # exact (category, parameter). Deliberately excludes sensor_fps_int
        # and frame_flags, matching what set_recording_format's own
        # verification compares (docs/ble/settings.md §12's table) — the
        # camera's own report of those two elements hasn't been
        # characterised. See set_recording_format's docstring.
        self.last_known_recording_format: tuple[int, int, int] | None = None

    async def __aenter__(self) -> CameraSession:
        discovered = await scan_for_camera(self.profile.ble_name)
        self._controller = BMDCameraController(discovered=discovered, profile=self.profile)
        await self._controller.connect()
        # Buffer before any write is possible — see NotificationRouter's docstring.
        await self._controller.subscribe_incoming(callback=self._handle_incoming)
        await self._controller.subscribe_timecode(callback=self._handle_timecode)
        # Let the just-connected camera's initial info dump settle before any
        # command can be sent — see module docstring and
        # docs/ble/session_and_verification.md for the real-hardware evidence.
        await asyncio.sleep(self.connect_settle_s)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._controller is not None:
            await self._controller.disconnect()

    def _handle_incoming(self, characteristic: Any, data: bytearray) -> None:
        """INCOMING_CONTROL callback: feeds the echo router, then watches for
        an unsolicited recording-state change, the CANDIDATE write-margin
        signal, the current codec/quality, and the current recording
        format. Never raises."""
        self._router.handle_incoming(characteristic, data)
        self._observe_recording_state(data)
        self._observe_write_margin(data)
        self._observe_codec_quality(data)
        self._observe_recording_format(data)

    def _observe_recording_format(self, data: bytearray) -> None:
        """Update `last_known_recording_format` from any recording_format-
        category report — see the module docstring, `set_recording_format`,
        and `set_video_format` (whose mode-notify confirmation is this exact
        (category, parameter), so a video_format write updates this too).
        Never raises: a profile without the recording_format block, or a
        packet this can't decode, is simply not this report."""
        try:
            spec = self.profile.require_command("recording_format")
        except ValueError:
            return
        try:
            header, payload = decode_packet(bytes(data))
        except ValueError:
            return
        if not is_settings_notification(header, category=spec.category, parameter=spec.parameter):
            return
        try:
            reported = decode_recording_format(payload, spec.data_type)
        except ValueError:
            return
        self.last_known_recording_format = (reported.fps_int, reported.width, reported.height)

    def _observe_codec_quality(self, data: bytearray) -> None:
        """Update `last_known_codec_variant` from any codec_quality-category
        report — see the module docstring and `set_codec_quality`. Never
        raises: a profile without the codec_quality block, or a packet this
        can't decode, is simply not this report."""
        try:
            spec = self.profile.require_command("codec_quality")
        except ValueError:
            return
        try:
            header, payload = decode_packet(bytes(data))
        except ValueError:
            return
        if not is_settings_notification(header, category=spec.category, parameter=spec.parameter):
            return
        try:
            self.last_known_codec_variant = decode_codec_quality(payload, spec.data_type)
        except ValueError:
            return

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
        (see docs/ble/recording.md's "Camera-initiated stop detection" section).
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
        out the rest of a recording that already ended. See docs/ble/recording.md.
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
        failure. See docs/ble/recording.md.
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
            # docs/ble/timecode.md.
            self.last_start_timecode = Timecode(hours=0, minutes=0, seconds=0, frames=0)
        else:
            self.last_stop_timecode = self._latest_timecode
            self.last_stop_reason = "requested"

    async def capture_photo(self) -> None:
        """Trigger a still photo capture, raising `BMDUnsupportedError`
        immediately unless the profile confirms `capabilities.supports_photo`.

        **Deliberate, loudly-documented exception to design principle 3**
        ("every write command must be verified before reporting success"):
        this method cannot verify anything. `docs/ble/photo_capture.md` §7
        confirms — independently, on `POCKET_6K_G2 v7.9` and
        `POCKET_6K_PRO v8.6` — that this trigger produces zero
        `INCOMING_CONTROL` footprint. Not a weak signal that needs a longer
        timeout: no signal exists at all. No echo, no `CAMERA_STATUS`
        movement, nothing this session's own transport could ever wait on.
        The one storage lead that does exist (`0x09/0x02`, §5.3) is far too
        coarse to attribute to *this* trigger specifically.

        This method's "success" therefore means exactly one thing: the
        trigger command was written to `OUTGOING_CONTROL`. It never means "a
        photo was confirmed taken" — this method never raises
        `BMDVerificationError`, because there is nothing here to time out on.
        Real confirmation is a REST-side concern, entirely outside what
        `CameraSession` (BLE-only, design principle 5) can compose with
        directly — see `rest/media.py` and `examples/capture_photo.py`,
        which poll for a new still file over REST after calling this method.

        Raises `ValueError` if the profile has no `photo` command block at
        all (e.g. `POCKET_6K_G2 v8.6` as of this writing — only `v7.9` and
        `POCKET_6K_PRO v8.6` have one; see `docs/ble/photo_capture.md` §7/§9
        and design principle 6 for why it cannot simply be copied from a
        sibling profile without re-verifying on this exact firmware).
        """
        if not self.profile.capabilities.get("supports_photo", False):
            raise BMDUnsupportedError(
                f"{self.profile.model_key} {self.profile.firmware} does not confirm "
                "photo capture support (capabilities.supports_photo) — see "
                "docs/ble/photo_capture.md"
            )
        spec = self.profile.require_command("photo")
        command = encode_photo_trigger(
            category=spec.category, parameter=spec.parameter, reserved=spec.reserved
        )
        await self._controller.write_outgoing_control(command)

    # ── Settings writes (CANDIDATE packet families — see docs/ble/settings.md) ──

    async def set_codec_quality(self, codec: str, variant: str) -> None:
        """Set the recording codec's quality variant, raising
        BMDVerificationError unless confirmed by echo.

        OBSERVED LIMITATION (see docs/ble/settings.md): this packet changes the
        quality variant within the *active* codec family but does NOT switch
        BRAW <-> ProRes, even though it carries a codec id — use
        `set_video_format` for a codec-family switch, then this for the
        variant.

        OBSERVED ON REAL HARDWARE (2026-07-20, docs/ble/settings.md §8, §11): the
        camera's 0x0A/0x00 report only fires on an actual applied change —
        requesting the (codec, variant) the camera is *already* at (e.g.
        right after `set_video_format` switches families, which resets the
        quality to a per-family remembered value) produces no report at
        all, which used to surface as a `BMDVerificationError` indistinguishable
        from a real failure. Mirrors `record_stop()`'s documented
        no-echo-on-redundant-command behavior (docs/ble/recording.md) — and now,
        like `record_stop()`, this is a no-op when `last_known_codec_variant`
        (notification-derived, never assumed) already confirms the target
        state: nothing is sent, no exception is raised. That guard only
        fires when a *prior* notification proved the state; the very first
        call in a session (before any codec_quality report has arrived)
        always writes and waits normally.
        """
        spec = self.profile.require_command("codec_quality")
        codec_spec = self.profile.require_codec(codec, variant)
        variant_id = codec_spec.variants[variant]
        if self.last_known_codec_variant == (codec_spec.id, variant_id):
            return
        command = encode_codec_quality(
            category=spec.category,
            parameter=spec.parameter,
            data_type=spec.data_type,
            codec_id=codec_spec.id,
            variant_id=variant_id,
            reserved=spec.reserved,
        )

        self._router.arm(spec.category, spec.parameter)
        await self._controller.write_outgoing_control(command)
        result = await self._router.wait_for(
            spec.category, spec.parameter, timeout=self.echo_timeout_s
        )
        if result is None:
            raise BMDVerificationError(
                f"set_codec_quality({codec} {variant}): no echo received within "
                f"{self.echo_timeout_s}s — either this family's echo behaviour is not "
                f"captured yet, or the camera was already at ({codec}, {variant}) without this "
                f"session having previously observed that (the known-redundant-write case is "
                f"normally caught before any write — see last_known_codec_variant) — "
                f"see docs/ble/settings.md"
            )
        _header, payload = result
        reported = decode_codec_quality(payload, spec.data_type)
        if reported != (codec_spec.id, variant_id):
            raise BMDVerificationError(
                f"set_codec_quality({codec} {variant}): echo reported "
                f"(codec_id, variant_id)={reported}, expected {(codec_spec.id, variant_id)} — "
                f"a codec-family mismatch here is the documented codec_quality limitation; "
                f"switch families with set_video_format first"
            )

    async def set_video_format(self, resolution: str, codec: str, fps: str) -> None:
        """Set resolution, codec family, and frame rate via the FORMAT
        packet, raising BMDVerificationError unless confirmed by echo.

        This is the packet whose dimension_enum locks resolution AND codec
        family together — the only known way to switch BRAW <-> ProRes (see
        docs/ble/settings.md). Raises BMDUnsupportedError when the profile says
        the camera doesn't offer `codec` at `resolution`, or when `fps`
        exceeds `resolution`'s `max_fps_int` (a real hardware ceiling, e.g.
        `POCKET_6K_PRO v8.6`'s '6K' topping out at 50 — confirmed absent
        from the camera's own UI, not a software gap like
        `known_unreachable`), and ValueError when the combination is
        supported but its dimension_enum hasn't been captured yet.

        The echo channel for this family is unconfirmed: the camera may
        report on the command's own (category, parameter), on the
        recording_format coordinates ("mode-notify"), or on codec_quality's
        coordinates (the camera reliably reports the new family's
        remembered quality after any video_format write, regardless of
        whether set_codec_quality is called separately) — all three that
        exist in the profile are armed and the first matching report is
        used for verification.

        KNOWN GAP, confirmed on real hardware (docs/ble/settings.md §10): the
        mode-notify payload encodes fps/width/height only, never codec — a
        codec-only switch (same resolution and fps, different family, e.g.
        4K DCI/ProRes -> 4K DCI/BRAW) produces a mode-notify report
        byte-identical to the one already seen before the write, which
        NotificationRouter's staleness filter then discards as a stale
        duplicate. Arming the codec_quality channel too closes this gap: a
        codec-only switch still changes what that channel reports, so it
        becomes the confirmation when mode-notify's content can't be.

        OBSERVED ON REAL HARDWARE (2026-07-21, docs/ble/settings.md §14, 7/7
        `--repeat 2` runs covering every combination of same/different
        family, resolution, and fps): the same silent-no-op behavior as
        `set_codec_quality` and `set_recording_format` — requesting the
        (resolution, codec, fps) the camera is already in produces no
        report on any of the three watched channels. Now a no-op when both
        `last_known_codec_variant` (its codec_id) and
        `last_known_recording_format` already confirm the target family and
        (fps_int, width, height) — reusing the same two notification-derived
        fields `set_codec_quality` and `set_recording_format` maintain,
        rather than tracking a third. The guard only fires once prior
        notifications have proven both halves of the state; before that it
        still writes and waits normally.
        """
        spec = self.profile.require_command("video_format")
        resolution_spec = self.profile.require_resolution(resolution)
        codec_spec = self.profile.require_codec(codec)
        fps_spec = self.profile.require_fps_mode(fps)

        if resolution_spec.codecs and codec not in resolution_spec.codecs:
            raise BMDUnsupportedError(
                f"{self.profile.model_key} {self.profile.firmware} does not offer codec "
                f"'{codec}' at '{resolution}' — supported there: "
                f"{', '.join(resolution_spec.codecs)}"
            )
        if (
            resolution_spec.max_fps_int is not None
            and fps_spec.fps_int > resolution_spec.max_fps_int
        ):
            raise BMDUnsupportedError(
                f"{self.profile.model_key} {self.profile.firmware} does not offer '{fps}' "
                f"(fps_int={fps_spec.fps_int}) at '{resolution}' — this resolution's fps "
                f"ceiling on this camera is {resolution_spec.max_fps_int} (a hardware "
                f"limit, confirmed absent from the camera's own UI, not a software gap — "
                f"see docs/ble/settings.md)"
            )
        dimension_enum = resolution_spec.dimension_enums.get(codec)
        if dimension_enum is None:
            raise ValueError(
                f"dimension_enum for '{resolution}' under '{codec}' has not been captured "
                f"on {self.profile.model_key} {self.profile.firmware} yet — enums never "
                f"appear in notifications, so probe candidates actively with "
                f"tools/control/send_settings_command.py --dimension-enum (see "
                f"docs/ble/settings.md) before using this combination."
            )

        already_satisfied = (
            self.last_known_codec_variant is not None
            and self.last_known_codec_variant[0] == codec_spec.id
            and self.last_known_recording_format
            == (fps_spec.fps_int, resolution_spec.width, resolution_spec.height)
        )
        if already_satisfied:
            return

        command = encode_video_format(
            category=spec.category,
            parameter=spec.parameter,
            data_type=spec.data_type,
            fps_int=fps_spec.fps_int,
            m_rate=fps_spec.m_rate,
            dimension_enum=dimension_enum,
            reserved=spec.reserved,
        )

        keys = [(spec.category, spec.parameter)]
        notify_spec = self.profile.command("recording_format")
        if notify_spec is not None:
            keys.append((notify_spec.category, notify_spec.parameter))
        codec_quality_spec = self.profile.command("codec_quality")
        if codec_quality_spec is not None:
            keys.append((codec_quality_spec.category, codec_quality_spec.parameter))
        for category, parameter in keys:
            self._router.arm(category, parameter)

        await self._controller.write_outgoing_control(command)
        hit = await self._wait_first_echo(keys, timeout=self.echo_timeout_s)
        if hit is None:
            raise BMDVerificationError(
                f"set_video_format({resolution} {codec} {fps}): no echo received on any of "
                f"{keys} within {self.echo_timeout_s}s — either the camera was already at "
                f"({resolution}, {codec}, {fps}) without this session having previously "
                f"observed both halves of that (the known-redundant-write case is normally "
                f"caught before any write — see last_known_codec_variant and "
                f"last_known_recording_format) or the write genuinely failed — see "
                f"docs/ble/settings.md"
            )

        key, (_header, payload) = hit
        if key == (spec.category, spec.parameter):
            reported = decode_video_format(payload, spec.data_type)
            expected = (fps_spec.fps_int, fps_spec.m_rate, dimension_enum)
            observed = (reported.fps_int, reported.m_rate, reported.dimension_enum)
            if observed != expected:
                raise BMDVerificationError(
                    f"set_video_format({resolution} {codec} {fps}): echo reported "
                    f"(fps_int, m_rate, dimension_enum)={observed}, expected {expected}"
                )
        elif notify_spec is not None and key == (notify_spec.category, notify_spec.parameter):
            reported_format = decode_recording_format(payload, notify_spec.data_type)
            expected = (fps_spec.fps_int, resolution_spec.width, resolution_spec.height)
            observed = (
                reported_format.fps_int,
                reported_format.width,
                reported_format.height,
            )
            if observed != expected:
                raise BMDVerificationError(
                    f"set_video_format({resolution} {codec} {fps}): mode-notify reported "
                    f"(fps_int, width, height)={observed}, expected {expected}"
                )
        else:
            reported_codec_id, _reported_variant_id = decode_codec_quality(
                payload, codec_quality_spec.data_type
            )
            if reported_codec_id != codec_spec.id:
                raise BMDVerificationError(
                    f"set_video_format({resolution} {codec} {fps}): codec_quality-channel "
                    f"reported codec_id={reported_codec_id}, expected {codec_spec.id}"
                )

    async def set_recording_format(
        self, resolution: str, fps: str, *, sensor_fps: str | None = None
    ) -> None:
        """Set resolution and frame rate via the recording-format packet
        (five int16 elements), raising BMDVerificationError unless confirmed
        by echo, or BMDUnsupportedError immediately when `fps` exceeds
        `resolution`'s `max_fps_int` (a real hardware ceiling — see
        `set_video_format`'s docstring for the same check and its
        real-hardware precedent).

        `sensor_fps` defaults to `fps` (off-speed recording untested). This
        packet does not carry a codec — the camera applies the dimensions
        under its active codec family; use `set_video_format` to switch
        families. Verification compares the echoed fps_int/width/height;
        sensor fps and frame_flags are not compared, since the camera's own
        report of those elements hasn't been characterised yet (see
        docs/ble/settings.md).

        OBSERVED ON REAL HARDWARE (2026-07-21, docs/ble/settings.md §14, 5/5
        `--repeat 2` runs): identical to `set_codec_quality`'s no-echo
        finding — requesting the (resolution, fps) the camera is already at
        produces no report at all. Like `set_codec_quality`, this is now a
        no-op when `last_known_recording_format` (notification-derived,
        updated from any recording_format-category report including
        `set_video_format`'s own mode-notify confirmations) already confirms
        the target `(fps_int, width, height)`: nothing is sent, no exception
        is raised. The guard only fires once a prior notification has proven
        the state; before that it still writes and waits normally.
        """
        spec = self.profile.require_command("recording_format")
        resolution_spec = self.profile.require_resolution(resolution)
        fps_spec = self.profile.require_fps_mode(fps)
        sensor_spec = self.profile.require_fps_mode(sensor_fps) if sensor_fps else fps_spec

        if (
            resolution_spec.max_fps_int is not None
            and fps_spec.fps_int > resolution_spec.max_fps_int
        ):
            raise BMDUnsupportedError(
                f"{self.profile.model_key} {self.profile.firmware} does not offer '{fps}' "
                f"(fps_int={fps_spec.fps_int}) at '{resolution}' — this resolution's fps "
                f"ceiling on this camera is {resolution_spec.max_fps_int} (a hardware "
                f"limit, confirmed absent from the camera's own UI, not a software gap — "
                f"see docs/ble/settings.md)"
            )

        if self.last_known_recording_format == (
            fps_spec.fps_int,
            resolution_spec.width,
            resolution_spec.height,
        ):
            return

        command = encode_recording_format(
            category=spec.category,
            parameter=spec.parameter,
            data_type=spec.data_type,
            fps_int=fps_spec.fps_int,
            sensor_fps_int=sensor_spec.fps_int,
            width=resolution_spec.width,
            height=resolution_spec.height,
            frame_flags=fps_spec.frame_flags,
            reserved=spec.reserved,
        )

        self._router.arm(spec.category, spec.parameter)
        await self._controller.write_outgoing_control(command)
        result = await self._router.wait_for(
            spec.category, spec.parameter, timeout=self.echo_timeout_s
        )
        if result is None:
            raise BMDVerificationError(
                f"set_recording_format({resolution} {fps}): no echo received within "
                f"{self.echo_timeout_s}s — either the camera was already at "
                f"({resolution}, {fps}) without this session having previously observed "
                f"that (the known-redundant-write case is normally caught before any "
                f"write — see last_known_recording_format) or the write genuinely "
                f"failed — see docs/ble/settings.md"
            )
        _header, payload = result
        reported = decode_recording_format(payload, spec.data_type)
        expected = (fps_spec.fps_int, resolution_spec.width, resolution_spec.height)
        observed = (reported.fps_int, reported.width, reported.height)
        if observed != expected:
            raise BMDVerificationError(
                f"set_recording_format({resolution} {fps}): echo reported "
                f"(fps_int, width, height)={observed}, expected {expected}"
            )

    def _closest_reachable_resolution(self, target: str, codec: str) -> str:
        """The resolution offering `codec` a known `dimension_enum` whose
        pixel dimensions are closest to `target`'s — the `video_format`
        "proxy" `set_camera_format` switches through when `target` itself
        has no known `dimension_enum` for `codec` (currently: 4K DCI under
        ProRes, which has no confirmed enum but is reachable in two steps
        via UHD — see docs/ble/settings.md §9). Distance is plain pixel-count
        difference (`|Δwidth| + |Δheight|`); with only two ProRes-enabled
        resolutions in the current profile (`HD`, `UHD`) this reliably picks
        `UHD` for a 4K DCI target, and the metric generalizes cleanly if a
        future profile adds more.
        """
        target_spec = self.profile.require_resolution(target)
        candidates = [
            (name, spec)
            for name, spec in self.profile.resolutions.items()
            if codec in spec.dimension_enums
        ]
        if not candidates:
            raise ValueError(
                f"No resolution with a known dimension_enum for codec '{codec}' exists in "
                f"{self.profile.model_key} {self.profile.firmware}'s profile — '{target}' "
                f"under '{codec}' isn't reachable via video_format at all yet."
            )
        name, _spec = min(
            candidates,
            key=lambda pair: (
                abs(pair[1].width - target_spec.width) + abs(pair[1].height - target_spec.height)
            ),
        )
        return name

    async def set_camera_format(self, codec: str, variant: str, resolution: str, fps: str) -> None:
        """Set codec family, quality variant, resolution, and frame rate
        together from one (codec, variant, resolution, fps) combination —
        the orchestration `CameraSession` exposes so a caller doesn't need
        to know which of the three settings packets (docs/ble/settings.md)
        accomplishes which part, or that one (resolution, codec) pair —
        currently only 4K DCI/ProRes — needs a two-step workaround.

        Sequence, each step already independently echo-verified by the
        method it calls (real-hardware evidence: docs/ble/settings.md §8-§9):

          1. `set_video_format(resolution, codec, fps)` if `resolution` has
             a known `dimension_enum` for `codec`. Otherwise — a
             `video_format` write can only select a (resolution, codec)
             pair it has a `dimension_enum` for — switch through the
             pixel-dimension-closest resolution `codec` *does* have one
             for instead (`_closest_reachable_resolution`), which gets the
             codec family right even though the resolution isn't the
             caller's target yet.
          2. `set_codec_quality(codec, variant)` — now that the codec
             family is confirmed active.
          3. `set_recording_format(resolution, fps)` — lands the caller's
             exact requested resolution and fps directly. This works even
             when step 1 only got the codec family right (not the
             resolution): `recording_format` encodes raw width/height, not
             a codec-locked enum, so it can retarget resolution within
             whatever family step 1 selected (confirmed on real hardware,
             docs/ble/settings.md §4.2's third run).

        Before any step runs, this method also checks the target resolution's
        `known_unreachable` map (`ResolutionSpec.known_unreachable`, profile
        `resolutions.<name>.known_unreachable`) — a codec listed there is one
        this camera demonstrably supports (confirmed reachable through its
        own body menu) but that every write-value hypothesis this codebase
        has tried still cannot reach over BLE (see docs/ble/settings.md, e.g.
        `POCKET_6K_PRO v8.6`'s ProRes/4K DCI gap, §16). If `codec` is listed
        there, this raises `BMDUnsupportedError` immediately — before
        connecting to any write path — rather than burning a full
        `set_video_format`/`set_codec_quality`/`set_recording_format`
        sequence (each with its own echo-timeout wait) on a combination
        already known to fail. This is a software capability gap, not a
        missing camera capability: it belongs in the profile precisely so
        it can be removed the moment a fix is found, without touching this
        method.

        This method adds no other verification of its own beyond what each
        step already does — a failure at any other step raises
        `BMDVerificationError` or `BMDUnsupportedError` from that step, and
        later steps don't run.

        Each step routinely lands the *next* step's target as a side
        effect — e.g. step 1's `video_format` write resets the codec
        family's quality to a per-family remembered value, which can
        already match the `variant` step 2 requests; step 1 alone can also
        already land the exact resolution+fps step 3 asks for. Every one of
        those redundant follow-up writes is a real-hardware-confirmed
        no-echo case (`docs/ble/settings.md` §11, §14) that used to be
        indistinguishable from `BMDVerificationError`. As of 2026-07-21 all
        three steps guard against it themselves via notification-derived
        `last_known_*` state (`last_known_codec_variant`,
        `last_known_recording_format`) — see each method's own docstring —
        so a redundant step here is silently skipped rather than raising.
        """
        resolution_spec = self.profile.require_resolution(resolution)
        self.profile.require_codec(codec, variant)

        unreachable_note = resolution_spec.known_unreachable.get(codec)
        if unreachable_note is not None:
            raise BMDUnsupportedError(
                f"{self.profile.model_key} {self.profile.firmware}: ({codec}, {resolution}) "
                f"is a known-unreachable combination over BLE — {unreachable_note}"
            )

        if codec in resolution_spec.dimension_enums:
            format_resolution = resolution
        else:
            format_resolution = self._closest_reachable_resolution(resolution, codec)

        await self.set_video_format(format_resolution, codec, fps)
        await self.set_codec_quality(codec, variant)
        await self.set_recording_format(resolution, fps)

    async def _wait_first_echo(
        self, keys: list[tuple[int, int]], *, timeout: float
    ) -> tuple[tuple[int, int], tuple[Any, bytes]] | None:
        """Await the first fresh router delivery on any of `keys`.

        Returns ((category, parameter), (header, payload)) for the first key
        that yields a fresh echo, or None when every key times out. Every
        key must already be armed. Used where a command's echo channel is
        not yet confirmed and more than one (category, parameter) may carry
        the camera's report — see set_video_format.
        """
        tasks = {
            asyncio.create_task(self._router.wait_for(category, parameter, timeout=timeout)): (
                category,
                parameter,
            )
            for category, parameter in keys
        }
        try:
            pending: set[asyncio.Task] = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    result = task.result()
                    if result is not None:
                        return tasks[task], result
            return None
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
