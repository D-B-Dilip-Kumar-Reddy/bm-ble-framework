"""Unit tests for tools/control/discover_command.py's pure logic.

No BLE, no input(), no hardware — matches tests/unit/'s "no hardware, full
mocking" rule and test_send_settings_command.py's sys.path pattern for
importing a standalone (non-package) tools/ script.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "common"))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "control"))

import discover_command as dc  # noqa: E402
from discovery import CandidateCommand, ConfirmedOutcome, build_command_block  # noqa: E402

from bmd_camera.ble.protocol.types import DataType  # noqa: E402

SAVED_PATH = Path("tools/captures/POCKET_6K_G2_v8.6/POCKET_6K_G2_v8.6_20260729T134321.json")


def _candidate(value: int, reserved: int) -> CandidateCommand:
    return CandidateCommand(
        category=0x0A,
        parameter=0x01,
        data_type=DataType.INT8,
        value=value,
        reserved=reserved,
    )


def _reserved_indifferent_confirmations() -> list[ConfirmedOutcome]:
    """The real POCKET_6K_G2 v8.6 recording sweep, 2026-07-29: the camera acted
    on both reserved bytes, so each outcome got two disagreeing confirmations.
    Candidate [1]'s window caught the post-connect burst, so it has no echo."""
    return [
        ConfirmedOutcome("start", _candidate(2, 0x01)),
        ConfirmedOutcome("stop", _candidate(0, 0x01), 2, "00 00 40 00 01 03"),
        ConfirmedOutcome("start", _candidate(2, 0x00), 2, "02 00 40 00 01 03"),
        ConfirmedOutcome("stop", _candidate(0, 0x00), 2, "00 00 40 00 01 03"),
    ]


class TestPrintUnemittableSummary:
    def test_reserved_indifferent_sweep_is_explained_not_raised(self, capsys):
        """A reserved-indifferent family cannot be emitted (one block carries a
        single scalar `reserved`), but that is a considered refusal, not a
        crash — the operator gets the reason and their evidence, not a
        traceback."""
        confirmed = _reserved_indifferent_confirmations()
        with pytest.raises(ValueError) as exc_info:
            build_command_block(
                name="recording",
                confirmed=confirmed,
                capture_ref=str(SAVED_PATH),
                discovered_on="2026-07-29",
            )

        dc.print_unemittable_summary(exc_info.value, confirmed, SAVED_PATH)

        out = capsys.readouterr().out
        assert "NO BLOCK EMITTED" in out
        assert "disagree on command coordinates" in out
        assert str(SAVED_PATH) in out, "the operator must be told their evidence is safe"

    def test_summary_lists_every_confirmation_with_its_echo_state(self, capsys):
        """The echo column is what the operator resolves the conflict on: the
        canonical reserved value is the one that echoed for every outcome."""
        confirmed = _reserved_indifferent_confirmations()
        with pytest.raises(ValueError) as exc_info:
            build_command_block(
                name="recording",
                confirmed=confirmed,
                capture_ref=None,
                discovered_on="2026-07-29",
            )

        dc.print_unemittable_summary(exc_info.value, confirmed, SAVED_PATH)

        out = capsys.readouterr().out
        assert out.count("start") >= 2 and out.count("stop") >= 2
        # Candidate [1] had no echo; the other three did.
        assert "NO ECHO CAPTURED" in out
        assert out.count("02 00 40 00 01 03") >= 1
        assert "reserved=0x01" in out and "reserved=0x00" in out

    def test_summary_names_both_readings_of_the_conflict(self, capsys):
        """The tool cannot tell a genuine reserved-indifference finding from an
        undiscriminating read, so the guidance must offer both, not assert one."""
        confirmed = _reserved_indifferent_confirmations()
        with pytest.raises(ValueError) as exc_info:
            build_command_block(
                name="recording",
                confirmed=confirmed,
                capture_ref=None,
                discovered_on="2026-07-29",
            )

        dc.print_unemittable_summary(exc_info.value, confirmed, SAVED_PATH)

        out = capsys.readouterr().out
        assert "by hand" in out
        assert "echoed for EVERY outcome" in out
        assert "re-run" in out
        assert "docs/ble/command_discovery.md" in out
