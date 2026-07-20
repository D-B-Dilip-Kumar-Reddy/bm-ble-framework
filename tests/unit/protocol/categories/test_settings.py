"""Unit tests for :mod:`bmd_ble.protocol.categories.settings`.

The byte-exact expectations mirror the reverse-engineered POCKET_6K_G2 v7.9
packet layouts recorded in docs/settings.md (external RE doc, CANDIDATE):

- codec_quality:      FF 06 00 00 0A 00 01 00 <codec_id> <variant_id>
- video_format:       FF 09 00 01 01 00 01 00 <fps> <m_rate> <dim_enum> 00 00
- recording_format:   FF 0E 00 01 01 09 82 00 <5 x int16 LE>
"""

import pytest

from bmd_ble.protocol.categories.settings import (
    RecordingFormat,
    VideoFormat,
    decode_codec_quality,
    decode_recording_format,
    decode_video_format,
    encode_codec_quality,
    encode_recording_format,
    encode_video_format,
    is_settings_notification,
)
from bmd_ble.protocol.codec import CommandHeader, Operation, decode_packet
from bmd_ble.protocol.types import DataType


def _hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


class TestEncodeCodecQuality:
    def test_braw_5_1_matches_documented_packet(self):
        # BRAW (codec_id 3), 5:1 (variant_id 3), reserved 0x00.
        packet = encode_codec_quality(
            category=0x0A,
            parameter=0x00,
            data_type=DataType.INT8,
            codec_id=3,
            variant_id=3,
            reserved=0x00,
        )
        assert _hex(packet) == "FF 06 00 00 0A 00 01 00 03 03"

    def test_prores_hq_matches_documented_packet(self):
        packet = encode_codec_quality(
            category=0x0A,
            parameter=0x00,
            data_type=DataType.INT8,
            codec_id=2,
            variant_id=0,
            reserved=0x00,
        )
        assert _hex(packet) == "FF 06 00 00 0A 00 01 00 02 00"

    def test_round_trips_through_decode_packet(self):
        packet = encode_codec_quality(
            category=0x0A,
            parameter=0x00,
            data_type=DataType.INT8,
            codec_id=3,
            variant_id=4,
            reserved=0x00,
        )
        header, payload = decode_packet(packet)
        assert header.category == 0x0A
        assert header.parameter == 0x00
        assert header.operation is Operation.ASSIGN
        assert decode_codec_quality(payload, DataType.INT8) == (3, 4)


class TestEncodeVideoFormat:
    def test_25fps_4k_dci_braw_matches_documented_packet(self):
        # 25 fps (0x19), exact rate, 4K DCI/BRAW dimension enum 0x08,
        # reserved 0x01, two trailing zero elements.
        packet = encode_video_format(
            category=0x01,
            parameter=0x00,
            data_type=DataType.INT8,
            fps_int=25,
            m_rate=0,
            dimension_enum=0x08,
            reserved=0x01,
        )
        assert _hex(packet) == "FF 09 00 01 01 00 01 00 19 00 08 00 00"

    def test_2398fps_hd_prores_matches_documented_packet(self):
        # 23.98 -> fps byte 0x18 with NTSC/drop m_rate 0x01; HD/ProRes enum 0x03.
        packet = encode_video_format(
            category=0x01,
            parameter=0x00,
            data_type=DataType.INT8,
            fps_int=24,
            m_rate=1,
            dimension_enum=0x03,
            reserved=0x01,
        )
        assert _hex(packet) == "FF 09 00 01 01 00 01 00 18 01 03 00 00"

    def test_round_trips_through_decode_packet(self):
        packet = encode_video_format(
            category=0x01,
            parameter=0x00,
            data_type=DataType.INT8,
            fps_int=60,
            m_rate=1,
            dimension_enum=0x13,
            reserved=0x01,
        )
        header, payload = decode_packet(packet)
        assert header.category == 0x01
        assert decode_video_format(payload, DataType.INT8) == VideoFormat(
            fps_int=60, m_rate=1, dimension_enum=0x13
        )


class TestEncodeRecordingFormat:
    def test_25fps_4k_dci_matches_documented_packet(self):
        # 25 fps file + sensor, 4096x2160, exact-rate flags 0x0010; the
        # data-type byte is the CANDIDATE 0x82 (INT16_ARRAY), reserved 0x01.
        packet = encode_recording_format(
            category=0x01,
            parameter=0x09,
            data_type=DataType.INT16_ARRAY,
            fps_int=25,
            sensor_fps_int=25,
            width=4096,
            height=2160,
            frame_flags=0x0010,
            reserved=0x01,
        )
        assert _hex(packet) == "FF 0E 00 01 01 09 82 00 19 00 19 00 00 10 70 08 10 00"

    def test_ntsc_flags_encode_little_endian(self):
        packet = encode_recording_format(
            category=0x01,
            parameter=0x09,
            data_type=DataType.INT16_ARRAY,
            fps_int=30,
            sensor_fps_int=30,
            width=6144,
            height=3456,
            frame_flags=0x0013,
            reserved=0x01,
        )
        # 6144 = 0x1800 -> 00 18; 3456 = 0x0D80 -> 80 0D; 0x0013 -> 13 00.
        assert _hex(packet) == "FF 0E 00 01 01 09 82 00 1E 00 1E 00 00 18 80 0D 13 00"

    def test_round_trips_through_decode_packet(self):
        packet = encode_recording_format(
            category=0x01,
            parameter=0x09,
            data_type=DataType.INT16_ARRAY,
            fps_int=50,
            sensor_fps_int=50,
            width=1920,
            height=1080,
            frame_flags=0x0010,
            reserved=0x01,
        )
        header, payload = decode_packet(packet)
        assert header.data_type is DataType.INT16_ARRAY
        assert decode_recording_format(payload, DataType.INT16_ARRAY) == RecordingFormat(
            fps_int=50, sensor_fps_int=50, width=1920, height=1080, frame_flags=0x0010
        )


class TestDecoders:
    def test_decode_codec_quality_ignores_trailing_bytes(self):
        assert decode_codec_quality(bytes([2, 1, 0xAA, 0xBB]), DataType.INT8) == (2, 1)

    def test_decode_video_format_ignores_trailing_bytes(self):
        payload = bytes([25, 0, 0x08, 0, 0, 0xEE])
        assert decode_video_format(payload, DataType.INT8) == VideoFormat(
            fps_int=25, m_rate=0, dimension_enum=0x08
        )

    def test_decode_codec_quality_rejects_short_payload(self):
        with pytest.raises(ValueError, match="at least 2-byte payload"):
            decode_codec_quality(bytes([2]), DataType.INT8)

    def test_decode_video_format_rejects_short_payload(self):
        with pytest.raises(ValueError, match="at least 5-byte payload"):
            decode_video_format(bytes([25, 0, 8]), DataType.INT8)

    def test_decode_recording_format_rejects_short_payload(self):
        with pytest.raises(ValueError, match="at least 10-byte payload"):
            decode_recording_format(bytes(8), DataType.INT16_ARRAY)

    def test_decoders_reject_unsupported_data_type(self):
        with pytest.raises(ValueError, match="Unsupported data type"):
            decode_codec_quality(bytes(2), DataType.STRING)
        with pytest.raises(ValueError, match="Unsupported data type"):
            decode_recording_format(bytes(10), DataType.VOID)

    def test_decodes_real_captured_recording_format_report(self):
        """Regression net against real POCKET_6K_G2 v7.9 bytes (2026-07-20
        passive capture): the camera's 1/9 report carries data-type byte
        0x02 (INT16, not the claimed 0x82 write byte) and the exact
        five-int16 element order — 50fps 4096x2160 flags 0x0010 here."""
        raw = bytes.fromhex("FF 0E 00 00 01 09 02 02 32 00 32 00 00 10 70 08 10 00")

        header, payload = decode_packet(raw)

        assert (header.category, header.parameter) == (0x01, 0x09)
        assert header.data_type is DataType.INT16
        assert header.operation is Operation.CAMERA_REPORT
        assert decode_recording_format(payload, header.data_type) == RecordingFormat(
            fps_int=50, sensor_fps_int=50, width=4096, height=2160, frame_flags=0x0010
        )

    def test_decodes_real_captured_full_sensor_flags(self):
        """Same capture, 6K 3:2 report — frame_flags 0x0000 at the same 50
        fps that reports 0x0010 elsewhere (the resolution-dependent
        'windowed' bit finding, docs/settings.md §5)."""
        raw = bytes.fromhex("FF 0E 00 00 01 09 02 02 32 00 32 00 00 18 80 0D 00 00")

        _header, payload = decode_packet(raw)

        assert decode_recording_format(payload, DataType.INT16) == RecordingFormat(
            fps_int=50, sensor_fps_int=50, width=6144, height=3456, frame_flags=0x0000
        )

    def test_decodes_real_captured_codec_report(self):
        """Same capture: the camera's 10/0 codec report — ProRes (2) HQ (0)
        after a body-initiated switch to ProRes."""
        raw = bytes.fromhex("FF 06 00 00 0A 00 01 02 02 00")

        header, payload = decode_packet(raw)

        assert (header.category, header.parameter) == (0x0A, 0x00)
        assert header.operation is Operation.CAMERA_REPORT
        assert decode_codec_quality(payload, header.data_type) == (2, 0)

    def test_decodes_real_captured_dimension_enum_probe_reports(self):
        """Regression net against the 2026-07-20 --dimension-enum probe
        sweep (docs/settings.md §7): each of these is the 0x01/0x09 report
        following a video_format write with the given enum, decoding to
        exactly the width/height already in the resolutions table — byte-
        exact confirmation for every known dimension_enum, all at 25fps."""
        cases = {
            "0x03 HD/ProRes": (
                "FF 0E 00 00 01 09 02 02 19 00 19 00 80 07 38 04 10 00",
                RecordingFormat(
                    fps_int=25, sensor_fps_int=25, width=1920, height=1080, frame_flags=0x0010
                ),
            ),
            "0x06 UHD/ProRes": (
                "FF 0E 00 00 01 09 02 02 19 00 19 00 00 0F 70 08 10 00",
                RecordingFormat(
                    fps_int=25, sensor_fps_int=25, width=3840, height=2160, frame_flags=0x0010
                ),
            ),
            "0x0D 2.8K/BRAW": (
                "FF 0E 00 00 01 09 02 02 19 00 19 00 34 0B E8 05 10 00",
                RecordingFormat(
                    fps_int=25, sensor_fps_int=25, width=2868, height=1512, frame_flags=0x0010
                ),
            ),
            "0x12 5.7K/BRAW": (
                "FF 0E 00 00 01 09 02 02 19 00 19 00 70 16 D0 0B 10 00",
                RecordingFormat(
                    fps_int=25, sensor_fps_int=25, width=5744, height=3024, frame_flags=0x0010
                ),
            ),
            "0x14 6K 2.4:1/BRAW": (
                "FF 0E 00 00 01 09 02 02 19 00 19 00 00 18 00 0A 10 00",
                RecordingFormat(
                    fps_int=25, sensor_fps_int=25, width=6144, height=2560, frame_flags=0x0010
                ),
            ),
        }
        for label, (hex_bytes, expected) in cases.items():
            header, payload = decode_packet(bytes.fromhex(hex_bytes))
            assert (header.category, header.parameter) == (0x01, 0x09), label
            assert decode_recording_format(payload, header.data_type) == expected, label

    def test_dimension_enum_0x10_report_is_indistinguishable_from_unchanged_state(self):
        """0x10 was probed 2026-07-20 (docs/settings.md §7) and refuted the
        earlier '3.7K Anamorphic alt' hypothesis: the resulting 0x01/0x09
        report is byte-identical to the prior (unrelated) 6K 2.4:1 report
        — i.e. the camera left the resolution unchanged rather than
        accepting 0x10 as a second enum for 3728x3104."""
        unchanged_after_0x10 = bytes.fromhex(
            "FF 0E 00 00 01 09 02 02 19 00 19 00 00 18 00 0A 10 00"
        )
        prior_6k_24_report = bytes.fromhex("FF 0E 00 00 01 09 02 02 19 00 19 00 00 18 00 0A 10 00")
        assert unchanged_after_0x10 == prior_6k_24_report

    def test_is_settings_notification_matches_on_category_and_parameter(self):
        header = CommandHeader(
            destination=0xFF,
            command_id=0x00,
            category=0x01,
            parameter=0x09,
            data_type=DataType.INT16_ARRAY,
            operation=Operation.CAMERA_REPORT,
        )
        assert is_settings_notification(header, category=0x01, parameter=0x09)
        assert not is_settings_notification(header, category=0x01, parameter=0x00)
