"""
bmd_camera/ble/protocol/categories/settings.py
========================================
Settings category families — codec/quality, video format (the FORMAT packet
that switches the codec family), and recording format (resolution + FPS).

WHAT BELONGS HERE
------------------
Only encode/decode logic for the three settings packet families
reverse-engineered on ``POCKET_6K_G2 v7.9``. Category, parameter, data
type, reserved byte, and every payload value (codec ids, variant ids,
dimension enums, fps encodings) are model/firmware-specific and must be
supplied by the caller from a ``CameraProfile`` — never hardcoded here
(CLAUDE.md design principles 1 and 6).

THE THREE FAMILIES (see docs/ble/settings.md for byte layouts and provenance)
-------------------------------------------------------------------------
- ``codec_quality`` — two int8 elements ``[codec_id, variant_id]``.
  Observed to change the quality variant within the active codec family
  but NOT to switch BRAW <-> ProRes, even though it carries a codec id.
- ``video_format`` — five int8 elements
  ``[fps_int, m_rate, dimension_enum, extra1, extra2]``. The
  ``dimension_enum`` encodes resolution AND codec family together — this
  is the packet that actually switches BRAW <-> ProRes. ``extra1``/
  ``extra2`` default to ``0`` — every real capture so far shows zero —
  and are unexplained (hypothesis: the official spec's video-mode
  ``interlaced`` and ``colorspace`` elements); ``encode_video_format``
  accepts nonzero overrides for discovery-grade probing of that
  hypothesis.
- ``recording_format`` — five little-endian int16 elements
  ``[fps_int, sensor_fps_int, width, height, frame_flags]``.

STATUS
------
All three families are CANDIDATE: byte layouts and value tables come from
an external reverse-engineering document for ``POCKET_6K_G2 v7.9`` and have
not yet been re-verified by this repo's capture tooling
(``tools/sniffers/sniffer_settings.py`` /
``tools/control/send_settings_command.py`` exist to do exactly that).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..codec import (
    RESERVED_BYTE,
    CommandHeader,
    Operation,
    encode_assign_elements,
    header_matches,
)
from ..types import DATA_TYPE_BYTE_WIDTHS, DATA_TYPE_STRUCT_FORMATS, DataType

# Element counts for the two five-element families. These are part of the
# packet *shape* (like the header layout in codec.py), not model-specific
# values — every capture of these families so far shows exactly five
# elements, and the official spec's video-mode/recording-format parameters
# are five-element structs too.
VIDEO_FORMAT_ELEMENT_COUNT = 5
RECORDING_FORMAT_ELEMENT_COUNT = 5


@dataclass(frozen=True)
class VideoFormat:
    """Decoded video-format (FORMAT packet) payload elements."""

    fps_int: int
    m_rate: int
    dimension_enum: int


@dataclass(frozen=True)
class RecordingFormat:
    """Decoded recording-format payload elements."""

    fps_int: int
    sensor_fps_int: int
    width: int
    height: int
    frame_flags: int


def encode_codec_quality(
    *,
    category: int,
    parameter: int,
    data_type: DataType,
    codec_id: int,
    variant_id: int,
    reserved: int = RESERVED_BYTE,
    operation: Operation = Operation.ASSIGN,
) -> bytes:
    """Encode a codec/quality command packet (``[codec_id, variant_id]``).

    All arguments must come from a ``CameraProfile`` (``commands.codec_quality``
    block plus the ``codecs`` lookup table) — never invented. Note this
    family has been observed NOT to switch the codec family on real
    hardware — see the module docstring. ``operation`` defaults to
    ``Operation.ASSIGN``, matching every write sent so far; overridable for
    discovery-grade probing (see ``tools/control/send_settings_command.py
    --operation``, docs/ble/settings.md §16).
    """
    return encode_assign_elements(
        category=category,
        parameter=parameter,
        data_type=data_type,
        values=[codec_id, variant_id],
        reserved=reserved,
        operation=operation,
    )


def encode_video_format(
    *,
    category: int,
    parameter: int,
    data_type: DataType,
    fps_int: int,
    m_rate: int,
    dimension_enum: int,
    reserved: int = RESERVED_BYTE,
    extra1: int = 0,
    extra2: int = 0,
    operation: Operation = Operation.ASSIGN,
) -> bytes:
    """Encode a video-format (FORMAT) command packet.

    Payload is ``[fps_int, m_rate, dimension_enum, extra1, extra2]`` —
    ``extra1``/``extra2`` default to ``0``, matching every observation so
    far (hypothesis: the official spec's ``interlaced`` and ``colorspace``
    video-mode elements, both zero for progressive YUV). Overridable for
    discovery-grade probing of that hypothesis (see
    ``tools/control/send_settings_command.py --video-format-extra``,
    docs/ble/settings.md §16) — no caller in this codebase passes anything but
    the default yet. ``dimension_enum`` locks resolution and codec family
    together; all other values come from ``CameraProfile``
    (``commands.video_format`` plus the ``resolutions``/``fps_modes``
    tables). ``operation`` defaults to ``Operation.ASSIGN``; overridable for
    the same discovery-grade reason (``--operation``, docs/ble/settings.md §16).
    """
    return encode_assign_elements(
        category=category,
        parameter=parameter,
        data_type=data_type,
        values=[fps_int, m_rate, dimension_enum, extra1, extra2],
        reserved=reserved,
        operation=operation,
    )


def encode_recording_format(
    *,
    category: int,
    parameter: int,
    data_type: DataType,
    fps_int: int,
    sensor_fps_int: int,
    width: int,
    height: int,
    frame_flags: int,
    reserved: int = RESERVED_BYTE,
    operation: Operation = Operation.ASSIGN,
) -> bytes:
    """Encode a recording-format command packet (five little-endian int16s).

    Payload is ``[fps_int, sensor_fps_int, width, height, frame_flags]``.
    All values come from ``CameraProfile`` (``commands.recording_format``
    plus the ``resolutions``/``fps_modes`` tables). ``operation`` defaults
    to ``Operation.ASSIGN``, matching every write sent so far; overridable
    for discovery-grade probing (see ``tools/control/send_settings_command.py
    --operation``, docs/ble/settings.md §16).
    """
    return encode_assign_elements(
        category=category,
        parameter=parameter,
        data_type=data_type,
        values=[fps_int, sensor_fps_int, width, height, frame_flags],
        reserved=reserved,
        operation=operation,
    )


def is_settings_notification(header: CommandHeader, *, category: int, parameter: int) -> bool:
    """Whether a decoded packet header matches a settings-family (category, parameter)."""
    return header_matches(header, category=category, parameter=parameter)


def _unpack_elements(payload: bytes, data_type: DataType, count: int, family: str) -> tuple:
    """Unpack the leading ``count`` same-typed elements of a payload.

    Like ``recording.decode_recording_state``, trailing extra bytes are
    ignored rather than treated as an error — camera-originated
    ``CAMERA_REPORT`` payloads have been observed to carry more bytes than
    the nominal element width elsewhere in this protocol.
    """
    fmt = DATA_TYPE_STRUCT_FORMATS.get(data_type)
    if fmt is None:
        raise ValueError(f"Unsupported data type for {family} payload: {data_type!r}")

    width = DATA_TYPE_BYTE_WIDTHS[data_type]
    needed = width * count
    if len(payload) < needed:
        raise ValueError(
            f"Expected at least {needed}-byte payload for {family} "
            f"({count} x {width}-byte elements), got {len(payload)} bytes"
        )
    return struct.unpack(f"<{count}{fmt}", payload[:needed])


def decode_codec_quality(payload: bytes, data_type: DataType) -> tuple[int, int]:
    """Decode a codec/quality payload into ``(codec_id, variant_id)``.

    Callers compare the ids against the profile's ``codecs`` table — this
    function attaches no meaning to them.
    """
    codec_id, variant_id = _unpack_elements(payload, data_type, 2, "codec_quality")
    return codec_id, variant_id


def decode_video_format(payload: bytes, data_type: DataType) -> VideoFormat:
    """Decode a video-format payload's meaningful leading elements.

    The two trailing elements (unexplained, observed ``0``) are decoded but
    not surfaced — nothing downstream may attach meaning to bytes that have
    none confirmed yet.
    """
    elements = _unpack_elements(payload, data_type, VIDEO_FORMAT_ELEMENT_COUNT, "video_format")
    fps_int, m_rate, dimension_enum = elements[:3]
    return VideoFormat(fps_int=fps_int, m_rate=m_rate, dimension_enum=dimension_enum)


def decode_recording_format(payload: bytes, data_type: DataType) -> RecordingFormat:
    """Decode a recording-format payload into its five named int16 elements."""
    fps_int, sensor_fps_int, width, height, frame_flags = _unpack_elements(
        payload, data_type, RECORDING_FORMAT_ELEMENT_COUNT, "recording_format"
    )
    return RecordingFormat(
        fps_int=fps_int,
        sensor_fps_int=sensor_fps_int,
        width=width,
        height=height,
        frame_flags=frame_flags,
    )
