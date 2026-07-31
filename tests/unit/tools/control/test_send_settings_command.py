"""Unit tests for tools/control/send_settings_command.py's --repeat,
--data-type, --video-format-extra, --operation, and --raw-payload flags —
pure argument-parsing, action-list-building, and resolution/override logic
only.

No BLE, no input(), no hardware — matches tests/unit/'s "no hardware, full
mocking" rule and tests/unit/tools/common/test_capture.py's sys.path
pattern for importing a standalone (non-package) tools/ script.
`CameraProfile.for_model()` reads local profile JSON only — no network/BLE
— so it's used directly for these tests, matching
tests/unit/tools/control/test_sweep_dimension_enum.py's precedent. The G2
profile is used rather than the PRO's so these tests can't become
collateral damage of the PRO's own profile values possibly changing later
as a result of the experiments these flags enable.
"""

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "control"))

import send_settings_command as ssc  # noqa: E402

from bmd_ble.camera_profile import CameraProfile  # noqa: E402
from bmd_ble.protocol.codec import Operation, encode_assign_elements  # noqa: E402
from bmd_ble.protocol.types import DataType  # noqa: E402


def _g2_profile() -> CameraProfile:
    return CameraProfile.for_model(model_key="POCKET_6K_G2", firmware="v7.9")


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(
        packet=None,
        codec=None,
        variant=None,
        resolution=None,
        fps=None,
        sensor_fps=None,
        dimension_enum=None,
        data_type=None,
        video_format_extra=None,
        operation=None,
        reserved=None,
        raw_payload=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestBuildRepeatedActions:
    def test_repeat_one_returns_label_unchanged(self):
        actions = ssc.build_repeated_actions("codec_quality BRAW 5:1", b"\x01\x02", 1)

        assert actions == [("codec_quality BRAW 5:1", b"\x01\x02")]

    def test_repeat_two_suffixes_each_label_with_send_index(self):
        actions = ssc.build_repeated_actions("recording_format 4K DCI 25", b"\xaa", 2)

        assert actions == [
            ("recording_format 4K DCI 25 (send 1/2)", b"\xaa"),
            ("recording_format 4K DCI 25 (send 2/2)", b"\xaa"),
        ]

    def test_repeat_two_uses_identical_command_bytes_for_every_send(self):
        command = b"\xff\x05\x00\x01\x0a\x00\x01\x00\x03\x03"

        actions = ssc.build_repeated_actions("codec_quality BRAW 5:1", command, 3)

        assert [cmd for _label, cmd in actions] == [command, command, command]

    def test_repeat_three_produces_three_uniquely_labeled_actions(self):
        actions = ssc.build_repeated_actions("video_format UHD ProRes 25", b"\x00", 3)

        labels = [label for label, _command in actions]
        assert labels == [
            "video_format UHD ProRes 25 (send 1/3)",
            "video_format UHD ProRes 25 (send 2/3)",
            "video_format UHD ProRes 25 (send 3/3)",
        ]
        assert len(set(labels)) == 3


class TestParseArgsRepeat:
    def _parse(self, monkeypatch, extra: list[str]) -> object:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "send_settings_command.py",
                "--model-key",
                "POCKET_6K_G2",
                "--firmware",
                "v7.9",
                "--packet",
                "codec_quality",
                "--codec",
                "BRAW",
                "--variant",
                "5:1",
                *extra,
            ],
        )
        return ssc.parse_args()

    def test_defaults_to_one_when_not_passed(self, monkeypatch):
        args = self._parse(monkeypatch, [])

        assert args.repeat == 1

    def test_explicit_repeat_is_parsed_as_int(self, monkeypatch):
        args = self._parse(monkeypatch, ["--repeat", "2"])

        assert args.repeat == 2


class TestParseArgsDataType:
    def _parse(self, monkeypatch, extra: list[str]) -> object:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "send_settings_command.py",
                "--model-key",
                "POCKET_6K_G2",
                "--firmware",
                "v7.9",
                "--packet",
                "codec_quality",
                "--codec",
                "BRAW",
                "--variant",
                "5:1",
                *extra,
            ],
        )
        return ssc.parse_args()

    def test_defaults_to_none_when_not_passed(self, monkeypatch):
        args = self._parse(monkeypatch, [])

        assert args.data_type is None

    def test_explicit_data_type_is_parsed_as_the_enum_name_string(self, monkeypatch):
        args = self._parse(monkeypatch, ["--data-type", "INT16"])

        assert args.data_type == "INT16"

    def test_invalid_data_type_choice_raises_systemexit(self, monkeypatch):
        with pytest.raises(SystemExit):
            self._parse(monkeypatch, ["--data-type", "NOT_A_TYPE"])


class TestResolveDataType:
    def test_returns_profile_data_type_when_override_is_none(self):
        spec = _g2_profile().require_command("recording_format")

        assert ssc.resolve_data_type(spec, _args(data_type=None)) == DataType.INT16_ARRAY

    def test_returns_overridden_data_type_when_given(self):
        spec = _g2_profile().require_command("recording_format")

        assert ssc.resolve_data_type(spec, _args(data_type="INT16")) == DataType.INT16


class TestBuildCommandDataTypeOverride:
    def test_recording_format_uses_profile_data_type_by_default(self):
        profile = _g2_profile()
        args = _args(packet="recording_format", resolution="4K DCI", fps="25")

        label, command = ssc.build_command(profile, args)

        assert command[6] == int(DataType.INT16_ARRAY)
        assert "override" not in label

    def test_recording_format_data_type_override_writes_0x02(self):
        profile = _g2_profile()
        args = _args(packet="recording_format", resolution="4K DCI", fps="25", data_type="INT16")

        label, command = ssc.build_command(profile, args)

        assert command[6] == int(DataType.INT16) == 0x02
        assert "INT16" in label
        assert "override" in label

    def test_video_format_data_type_override_changes_wire_byte(self):
        profile = _g2_profile()
        args = _args(
            packet="video_format",
            resolution="UHD",
            codec="ProRes",
            fps="25",
            data_type="INT32",
        )

        _label, command = ssc.build_command(profile, args)

        assert command[6] == int(DataType.INT32)

    def test_codec_quality_data_type_override_changes_wire_byte(self):
        profile = _g2_profile()
        args = _args(packet="codec_quality", codec="BRAW", variant="5:1", data_type="INT16")

        _label, command = ssc.build_command(profile, args)

        assert command[6] == int(DataType.INT16)

    def test_no_override_leaves_wire_bytes_identical_to_pre_flag_behavior(self):
        profile = _g2_profile()
        cases = [
            ("codec_quality", _args(packet="codec_quality", codec="BRAW", variant="5:1")),
            (
                "video_format",
                _args(packet="video_format", resolution="UHD", codec="ProRes", fps="25"),
            ),
            (
                "recording_format",
                _args(packet="recording_format", resolution="4K DCI", fps="25"),
            ),
        ]

        for name, args in cases:
            spec = profile.require_command(name)
            _label, command = ssc.build_command(profile, args)
            assert command[6] == int(spec.data_type)


class TestParseArgsVideoFormatExtra:
    def _parse(self, monkeypatch, extra: list[str]) -> object:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "send_settings_command.py",
                "--model-key",
                "POCKET_6K_G2",
                "--firmware",
                "v7.9",
                "--packet",
                "video_format",
                "--resolution",
                "UHD",
                "--codec",
                "ProRes",
                "--fps",
                "25",
                *extra,
            ],
        )
        return ssc.parse_args()

    def test_defaults_to_none_when_not_passed(self, monkeypatch):
        args = self._parse(monkeypatch, [])

        assert args.video_format_extra is None

    def test_explicit_pair_is_parsed_as_two_ints(self, monkeypatch):
        args = self._parse(monkeypatch, ["--video-format-extra", "1", "0"])

        assert args.video_format_extra == [1, 0]

    def test_accepts_hex_values(self, monkeypatch):
        args = self._parse(monkeypatch, ["--video-format-extra", "0x0A", "0x0B"])

        assert args.video_format_extra == [0x0A, 0x0B]

    def test_missing_second_value_raises_systemexit(self, monkeypatch):
        with pytest.raises(SystemExit):
            self._parse(monkeypatch, ["--video-format-extra", "1"])


class TestResolveVideoFormatExtra:
    def test_returns_zero_pair_when_override_is_none(self):
        assert ssc.resolve_video_format_extra(_args(video_format_extra=None)) == (0, 0)

    def test_returns_overridden_pair_when_given(self):
        assert ssc.resolve_video_format_extra(_args(video_format_extra=[1, 2])) == (1, 2)


class TestBuildCommandVideoFormatExtraOverride:
    def test_defaults_to_zero_trailing_elements(self):
        profile = _g2_profile()
        args = _args(packet="video_format", resolution="UHD", codec="ProRes", fps="25")

        label, command = ssc.build_command(profile, args)

        assert command[-2:] == b"\x00\x00"
        assert "override" not in label

    def test_override_writes_the_given_trailing_elements(self):
        profile = _g2_profile()
        args = _args(
            packet="video_format",
            resolution="UHD",
            codec="ProRes",
            fps="25",
            video_format_extra=[1, 2],
        )

        label, command = ssc.build_command(profile, args)

        assert command[-2:] == b"\x01\x02"
        assert "extra=(1,2)" in label
        assert "override" in label

    def test_composes_with_dimension_enum_probe_mode(self):
        profile = _g2_profile()
        args = _args(
            packet="video_format",
            fps="25",
            dimension_enum=0x08,
            video_format_extra=[1, 0],
        )

        label, command = ssc.build_command(profile, args)

        assert command[-2:] == b"\x01\x00"
        assert "probe enum=0x08" in label
        assert "extra=(1,0)" in label


class TestParseArgsOperation:
    def _parse(self, monkeypatch, extra: list[str]) -> object:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "send_settings_command.py",
                "--model-key",
                "POCKET_6K_G2",
                "--firmware",
                "v7.9",
                "--packet",
                "codec_quality",
                "--codec",
                "BRAW",
                "--variant",
                "5:1",
                *extra,
            ],
        )
        return ssc.parse_args()

    def test_defaults_to_none_when_not_passed(self, monkeypatch):
        args = self._parse(monkeypatch, [])

        assert args.operation is None

    def test_explicit_operation_is_parsed_as_the_enum_name_string(self, monkeypatch):
        args = self._parse(monkeypatch, ["--operation", "OFFSET"])

        assert args.operation == "OFFSET"

    def test_invalid_operation_choice_raises_systemexit(self, monkeypatch):
        with pytest.raises(SystemExit):
            self._parse(monkeypatch, ["--operation", "NOT_AN_OPERATION"])


class TestResolveOperation:
    def test_returns_assign_when_override_is_none(self):
        assert ssc.resolve_operation(_args(operation=None)) == Operation.ASSIGN

    def test_returns_overridden_operation_when_given(self):
        assert ssc.resolve_operation(_args(operation="OFFSET")) == Operation.OFFSET


class TestBuildCommandReservedOverride:
    """`--reserved` (added 2026-07-30). The reserved byte is the least-evidenced
    field in a passively-seeded CANDIDATE block — a camera's own reports need
    not carry the value a *write* requires — so it must be varyable without
    editing a profile. Header byte 3 is the reserved byte."""

    def test_recording_format_uses_profile_reserved_by_default(self):
        profile = _g2_profile()
        args = _args(packet="recording_format", resolution="4K DCI", fps="25")

        label, command = ssc.build_command(profile, args)

        assert command[3] == profile.require_command("recording_format").reserved
        assert "reserved=" not in label

    def test_recording_format_reserved_override_changes_wire_byte(self):
        profile = _g2_profile()
        spec = profile.require_command("recording_format")
        assert spec.reserved == 1, "fixture assumption: v7.9 writes this family with 0x01"
        args = _args(packet="recording_format", resolution="4K DCI", fps="25", reserved=0)

        label, command = ssc.build_command(profile, args)

        assert command[3] == 0x00
        assert "reserved=0x00" in label
        assert "override" in label
        assert "profile default 0x01" in label

    def test_video_format_reserved_override_changes_wire_byte(self):
        profile = _g2_profile()
        args = _args(packet="video_format", resolution="UHD", codec="ProRes", fps="25", reserved=0)

        _label, command = ssc.build_command(profile, args)

        assert command[3] == 0x00

    def test_codec_quality_reserved_override_changes_wire_byte(self):
        profile = _g2_profile()
        args = _args(packet="codec_quality", codec="BRAW", variant="5:1", reserved=1)

        label, command = ssc.build_command(profile, args)

        assert command[3] == 0x01
        assert "reserved=0x01" in label

    def test_reserved_override_composes_with_the_other_overrides(self):
        """All four override suffixes must be able to appear together — a
        discovery send often needs to vary more than one axis at once."""
        profile = _g2_profile()
        args = _args(
            packet="video_format",
            resolution="UHD",
            codec="ProRes",
            fps="25",
            reserved=0,
            data_type="INT16",
            operation="OFFSET",
        )

        label, command = ssc.build_command(profile, args)

        assert command[3] == 0x00
        assert "reserved=0x00" in label
        assert "data_type=INT16" in label
        assert "operation=OFFSET" in label

    @pytest.mark.parametrize(("raw", "expected"), [("0x01", 1), ("1", 1), ("0x00", 0), ("0", 0)])
    def test_parse_args_accepts_hex_and_decimal_reserved(self, monkeypatch, raw, expected):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "send_settings_command.py",
                "--model-key",
                "POCKET_6K_G2",
                "--firmware",
                "v8.6",
                "--packet",
                "video_format",
                "--fps",
                "24",
                "--dimension-enum",
                "0x08",
                "--reserved",
                raw,
            ],
        )

        assert ssc.parse_args().reserved == expected

    def test_parse_args_defaults_reserved_to_none(self, monkeypatch):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "send_settings_command.py",
                "--model-key",
                "POCKET_6K_G2",
                "--firmware",
                "v8.6",
                "--packet",
                "codec_quality",
                "--codec",
                "BRAW",
                "--variant",
                "5:1",
            ],
        )

        assert ssc.parse_args().reserved is None


class TestBuildCommandOperationOverride:
    def test_recording_format_uses_assign_by_default(self):
        profile = _g2_profile()
        args = _args(packet="recording_format", resolution="4K DCI", fps="25")

        label, command = ssc.build_command(profile, args)

        assert command[7] == int(Operation.ASSIGN)
        assert "operation=" not in label

    def test_recording_format_operation_override_writes_offset(self):
        profile = _g2_profile()
        args = _args(packet="recording_format", resolution="4K DCI", fps="25", operation="OFFSET")

        label, command = ssc.build_command(profile, args)

        assert command[7] == int(Operation.OFFSET) == 0x01
        assert "operation=OFFSET" in label
        assert "override" in label

    def test_video_format_operation_override_changes_wire_byte(self):
        profile = _g2_profile()
        args = _args(
            packet="video_format",
            resolution="UHD",
            codec="ProRes",
            fps="25",
            operation="OFFSET",
        )

        _label, command = ssc.build_command(profile, args)

        assert command[7] == int(Operation.OFFSET)

    def test_codec_quality_operation_override_changes_wire_byte(self):
        profile = _g2_profile()
        args = _args(packet="codec_quality", codec="BRAW", variant="5:1", operation="OFFSET")

        _label, command = ssc.build_command(profile, args)

        assert command[7] == int(Operation.OFFSET)

    def test_no_override_leaves_operation_byte_identical_to_pre_flag_behavior(self):
        profile = _g2_profile()
        cases = [
            _args(packet="codec_quality", codec="BRAW", variant="5:1"),
            _args(packet="video_format", resolution="UHD", codec="ProRes", fps="25"),
            _args(packet="recording_format", resolution="4K DCI", fps="25"),
        ]

        for args in cases:
            _label, command = ssc.build_command(profile, args)
            assert command[7] == int(Operation.ASSIGN)

    def test_composes_with_data_type_and_video_format_extra_overrides(self):
        profile = _g2_profile()
        args = _args(
            packet="video_format",
            resolution="UHD",
            codec="ProRes",
            fps="25",
            video_format_extra=[1, 0],
            operation="OFFSET",
        )

        label, command = ssc.build_command(profile, args)

        assert command[-2:] == b"\x01\x00"
        assert command[7] == int(Operation.OFFSET)
        assert "extra=(1,0)" in label
        assert "operation=OFFSET" in label


class TestParseArgsRawPayload:
    def _parse(self, monkeypatch, extra: list[str]) -> object:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "send_settings_command.py",
                "--model-key",
                "POCKET_6K_G2",
                "--firmware",
                "v7.9",
                "--packet",
                "recording_format",
                *extra,
            ],
        )
        return ssc.parse_args()

    def test_defaults_to_none_when_not_passed(self, monkeypatch):
        args = self._parse(monkeypatch, ["--resolution", "4K DCI", "--fps", "25"])

        assert args.raw_payload is None

    def test_explicit_values_are_parsed_as_ints(self, monkeypatch):
        args = self._parse(monkeypatch, ["--raw-payload", "0", "0", "256", "0", "0"])

        assert args.raw_payload == [0, 0, 256, 0, 0]

    def test_accepts_hex_values(self, monkeypatch):
        args = self._parse(monkeypatch, ["--raw-payload", "0x00", "0x100"])

        assert args.raw_payload == [0x00, 0x100]

    def test_no_values_raises_systemexit(self, monkeypatch):
        with pytest.raises(SystemExit):
            self._parse(monkeypatch, ["--raw-payload"])


class TestBuildCommandRawPayload:
    def test_matches_direct_encode_assign_elements_call(self):
        profile = _g2_profile()
        spec = profile.require_command("recording_format")
        args = _args(packet="recording_format", raw_payload=[0, 0, 256, 0, 0])

        label, command = ssc.build_command(profile, args)

        expected = encode_assign_elements(
            category=spec.category,
            parameter=spec.parameter,
            data_type=spec.data_type,
            values=[0, 0, 256, 0, 0],
            reserved=spec.reserved,
            operation=Operation.ASSIGN,
        )
        assert command == expected
        assert "raw_payload=[0, 0, 256, 0, 0]" in label

    def test_bypasses_resolution_and_fps_lookup(self):
        # No --resolution/--fps given at all — a raw-payload send must not
        # need them, unlike every other recording_format build.
        profile = _g2_profile()
        args = _args(packet="recording_format", raw_payload=[1, 2, 3, 4, 5])

        _label, command = ssc.build_command(profile, args)

        spec = profile.require_command("recording_format")
        assert command[4] == spec.category
        assert command[5] == spec.parameter

    def test_composes_with_operation_override(self):
        profile = _g2_profile()
        args = _args(
            packet="recording_format",
            raw_payload=[0, 0, 256, 0, 0],
            operation="OFFSET",
        )

        label, command = ssc.build_command(profile, args)

        assert command[7] == int(Operation.OFFSET)
        assert "operation=OFFSET" in label

    def test_composes_with_data_type_override(self):
        profile = _g2_profile()
        args = _args(
            packet="recording_format",
            raw_payload=[0, 0, 256, 0, 0],
            data_type="INT16",
        )

        label, command = ssc.build_command(profile, args)

        assert command[6] == int(DataType.INT16)
        assert "data_type=INT16" in label

    def test_takes_priority_over_per_packet_flags(self):
        # Even if --resolution/--fps/--codec/--variant are also set, a
        # given --raw-payload short-circuits build_command entirely.
        profile = _g2_profile()
        args = _args(
            packet="codec_quality",
            codec="BRAW",
            variant="5:1",
            raw_payload=[9, 9],
        )

        _label, command = ssc.build_command(profile, args)

        spec = profile.require_command("codec_quality")
        expected = encode_assign_elements(
            category=spec.category,
            parameter=spec.parameter,
            data_type=spec.data_type,
            values=[9, 9],
            reserved=spec.reserved,
            operation=Operation.ASSIGN,
        )
        assert command == expected
