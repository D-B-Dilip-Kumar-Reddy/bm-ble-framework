"""Unit tests for tools/control/send_settings_command.py's --repeat and
--data-type flags — pure argument-parsing, action-list-building, and
data_type-resolution logic only.

No BLE, no input(), no hardware — matches tests/unit/'s "no hardware, full
mocking" rule and tests/unit/tools/common/test_capture.py's sys.path
pattern for importing a standalone (non-package) tools/ script.
`CameraProfile.for_model()` reads local profile JSON only — no network/BLE
— so it's used directly for the --data-type tests, matching
tests/unit/tools/control/test_sweep_dimension_enum.py's precedent. The G2
profile is used rather than the PRO's so these tests can't become
collateral damage of the PRO's own recording_format.data_type value
possibly changing later as a result of the experiment this flag enables.
"""

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "control"))

import send_settings_command as ssc  # noqa: E402

from bmd_ble.camera_profile import CameraProfile  # noqa: E402
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
