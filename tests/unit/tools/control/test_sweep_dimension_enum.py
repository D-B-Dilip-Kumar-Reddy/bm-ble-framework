"""Unit tests for tools/control/sweep_dimension_enum.py's pure logic —
candidate-range computation, decode/match helpers, and argument parsing.

No BLE, no input(), no hardware — matches tests/unit/'s "no hardware, full
mocking" rule and tests/unit/tools/control/test_send_settings_command.py's
sys.path pattern for importing a standalone (non-package) tools/ script.
CameraProfile.for_model() reads local profile JSON only — no network/BLE —
so it's used directly here rather than hand-building a CameraProfile, the
same way tests/unit/test_camera_profile.py exercises real profiles.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "common"))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "control"))

import sweep_dimension_enum as sde  # noqa: E402
from capture import CaptureWindow, DecodedNotification  # noqa: E402
from discovery import INCOMING_CONTROL_NAME  # noqa: E402

from bmd_camera.ble.protocol.categories.settings import RecordingFormat  # noqa: E402
from bmd_camera.camera_profile import CameraProfile  # noqa: E402


def _g2_profile() -> CameraProfile:
    return CameraProfile.for_model(model_key="POCKET_6K_G2", firmware="v7.9")


def _notification(
    *,
    category: int | None,
    parameter: int | None,
    data_type: str | None = "INT16_ARRAY",
    payload_hex: str | None = "19 00 19 00 00 10 70 08 10 00",
    characteristic_name: str = INCOMING_CONTROL_NAME,
    decode_error: str | None = None,
) -> DecodedNotification:
    return DecodedNotification(
        timestamp="2026-07-22T00:00:00.000",
        characteristic_uuid="uuid",
        characteristic_name=characteristic_name,
        raw_hex="FF 00",
        category=category,
        parameter=parameter,
        data_type=data_type,
        operation="CAMERA_REPORT",
        payload_hex=payload_hex,
        decode_error=decode_error,
    )


class TestParseIntList:
    def test_parses_hex_and_decimal(self):
        assert sde.parse_int_list("0x08,20,0x0F", "--enums") == [8, 20, 15]

    def test_rejects_garbage(self):
        try:
            sde.parse_int_list("not-a-number", "--enums")
        except SystemExit as exc:
            assert "--enums" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


class TestKnownDimensionEnums:
    def test_collects_every_enum_across_every_codec(self):
        known = sde.known_dimension_enums(_g2_profile())

        assert known == {19, 20, 18, 8, 15, 13, 6, 3}


class TestComputeCandidates:
    def test_explicit_enums_used_verbatim_when_not_excluding_known(self):
        args = SimpleNamespace(enums="0x08,0x09,0x0A", range=None, include_known=True)

        assert sde.compute_candidates(args, _g2_profile()) == [8, 9, 10]

    def test_default_range_excludes_known_enums(self):
        args = SimpleNamespace(enums=None, range=None, include_known=False)

        candidates = sde.compute_candidates(args, _g2_profile())

        assert 8 not in candidates  # 4K DCI/BRAW, already known
        assert 3 not in candidates  # HD/ProRes, already known
        assert 9 in candidates  # untried gap
        assert candidates == sorted(candidates)

    def test_include_known_keeps_everything_in_range(self):
        args = SimpleNamespace(enums=None, range=(0x00, 0x08), include_known=True)

        assert sde.compute_candidates(args, _g2_profile()) == list(range(0x00, 0x09))

    def test_custom_range_is_honored(self):
        args = SimpleNamespace(enums=None, range=(0x0A, 0x0C), include_known=False)

        assert sde.compute_candidates(args, _g2_profile()) == [0x0A, 0x0B, 0x0C]

    def test_explicit_enums_still_deduplicated(self):
        args = SimpleNamespace(enums="0x08,0x08,0x09", range=None, include_known=True)

        assert sde.compute_candidates(args, _g2_profile()) == [8, 9]


class TestLatestDecoded:
    def test_returns_none_when_nothing_matches(self):
        window = CaptureWindow(label="probe", notifications=[])

        assert sde.latest_decoded(window, category=1, parameter=9) is None

    def test_skips_decode_errors_and_wrong_characteristic(self):
        window = CaptureWindow(
            label="probe",
            notifications=[
                _notification(category=1, parameter=9, decode_error="boom"),
                _notification(category=1, parameter=9, characteristic_name="CAMERA_STATUS"),
            ],
        )

        assert sde.latest_decoded(window, category=1, parameter=9) is None

    def test_returns_the_latest_of_several_matches(self):
        first = _notification(category=1, parameter=9, payload_hex="AA")
        second = _notification(category=1, parameter=9, payload_hex="BB")
        window = CaptureWindow(label="probe", notifications=[first, second])

        assert sde.latest_decoded(window, category=1, parameter=9) is second

    def test_ignores_notifications_for_other_triples(self):
        window = CaptureWindow(
            label="probe", notifications=[_notification(category=9, parameter=0)]
        )

        assert sde.latest_decoded(window, category=1, parameter=9) is None


class TestResultDecoder:
    def test_decodes_recording_format_and_codec_quality_from_a_window(self):
        decoder = sde.ResultDecoder(_g2_profile())
        window = CaptureWindow(
            label="probe",
            notifications=[
                _notification(
                    category=1,
                    parameter=9,
                    data_type="INT16_ARRAY",
                    payload_hex="19 00 19 00 00 10 70 08 10 00",
                ),
                _notification(
                    category=10,
                    parameter=0,
                    data_type="INT8",
                    payload_hex="02 01",
                ),
            ],
        )

        recording_format, codec_quality = decoder.decode(window)

        assert recording_format == RecordingFormat(
            fps_int=25, sensor_fps_int=25, width=4096, height=2160, frame_flags=16
        )
        assert codec_quality == (2, 1)

    def test_missing_report_decodes_to_none(self):
        decoder = sde.ResultDecoder(_g2_profile())
        window = CaptureWindow(label="probe", notifications=[])

        assert decoder.decode(window) == (None, None)


class TestDescribeResult:
    def test_no_reports_at_all(self):
        assert sde.describe_result(None, None) == "(no recording_format/codec_quality report)"

    def test_includes_both_reports_when_present(self):
        rf = RecordingFormat(fps_int=25, sensor_fps_int=25, width=4096, height=2160, frame_flags=16)

        text = sde.describe_result(rf, (2, 1))

        assert "4096x2160" in text
        assert "codec_id=2 variant_id=1" in text


class TestIsMatch:
    RF = RecordingFormat(fps_int=25, sensor_fps_int=25, width=4096, height=2160, frame_flags=16)

    def test_no_target_resolution_never_matches(self):
        assert not sde.is_match(
            self.RF, (2, 1), target_width=None, target_height=None, target_codec_id=None
        )

    def test_no_report_never_matches(self):
        assert not sde.is_match(
            None, None, target_width=4096, target_height=2160, target_codec_id=None
        )

    def test_wrong_dimensions_do_not_match(self):
        assert not sde.is_match(
            self.RF, None, target_width=3840, target_height=2160, target_codec_id=None
        )

    def test_matching_dimensions_with_no_codec_target_matches(self):
        assert sde.is_match(
            self.RF, None, target_width=4096, target_height=2160, target_codec_id=None
        )

    def test_matching_dimensions_but_wrong_codec_does_not_match(self):
        assert not sde.is_match(
            self.RF, (3, 5), target_width=4096, target_height=2160, target_codec_id=2
        )

    def test_matching_dimensions_and_codec_matches(self):
        assert sde.is_match(
            self.RF, (2, 1), target_width=4096, target_height=2160, target_codec_id=2
        )

    def test_codec_target_with_no_codec_quality_report_does_not_match(self):
        assert not sde.is_match(
            self.RF, None, target_width=4096, target_height=2160, target_codec_id=2
        )


class TestRecordingFormatState:
    RF = RecordingFormat(fps_int=25, sensor_fps_int=25, width=4096, height=2160, frame_flags=16)

    def test_none_report_gives_none_state(self):
        assert sde.recording_format_state(None) is None

    def test_extracts_width_height_flags_fingerprint(self):
        assert sde.recording_format_state(self.RF) == (4096, 2160, 16)

    def test_same_dimensions_different_flags_are_distinct_states(self):
        windowed = RecordingFormat(
            fps_int=25, sensor_fps_int=25, width=1920, height=1080, frame_flags=0x10
        )
        unwindowed = RecordingFormat(
            fps_int=25, sensor_fps_int=25, width=1920, height=1080, frame_flags=0x00
        )
        assert sde.recording_format_state(windowed) != sde.recording_format_state(unwindowed)


class TestParseArgs:
    def _parse(self, monkeypatch, extra: list[str]) -> object:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "sweep_dimension_enum.py",
                "--model-key",
                "POCKET_6K_PRO",
                "--firmware",
                "v8.6",
                "--fps",
                "25",
                *extra,
            ],
        )
        return sde.parse_args()

    def test_defaults(self, monkeypatch):
        args = self._parse(monkeypatch, [])

        assert args.enums is None
        assert args.range is None
        assert args.include_known is False
        assert args.target_resolution is None
        assert args.target_codec is None
        assert args.stop_on_match is True
        assert args.restore_enum is None
        assert args.listen_seconds == 3.0
        assert args.pause_seconds == 1.5
        assert args.connect_settle_seconds == 6.0

    def test_no_stop_on_match_flips_the_default(self, monkeypatch):
        args = self._parse(monkeypatch, ["--no-stop-on-match"])

        assert args.stop_on_match is False

    def test_range_parses_hex_bounds(self, monkeypatch):
        args = self._parse(monkeypatch, ["--range", "0x00", "0x16"])

        assert args.range == [0, 22]

    def test_restore_enum_parses_hex(self, monkeypatch):
        args = self._parse(monkeypatch, ["--restore-enum", "0x06"])

        assert args.restore_enum == 6
