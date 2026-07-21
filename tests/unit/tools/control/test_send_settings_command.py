"""Unit tests for tools/control/send_settings_command.py's --repeat flag —
pure argument-parsing and action-list-building logic only.

No BLE, no input(), no hardware — matches tests/unit/'s "no hardware, full
mocking" rule and tests/unit/tools/common/test_capture.py's sys.path
pattern for importing a standalone (non-package) tools/ script.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "control"))

import send_settings_command as ssc  # noqa: E402


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
