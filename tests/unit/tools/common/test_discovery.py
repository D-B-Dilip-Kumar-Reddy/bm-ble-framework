"""Unit tests for tools/common/discovery.py — pure discovery logic only.

No BLE, no input(), no hardware. tools/ is not a package, so tools/common is
added to sys.path directly (same pattern as test_capture.py).
"""

import json
import sys
from pathlib import Path

import jsonschema
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "common"))

from discovery import (  # noqa: E402
    INCOMING_CONTROL_NAME,
    CandidateCommand,
    ConfirmedOutcome,
    build_command_block,
    extract_echo,
    generate_candidates,
    render_profile_snippet,
    seed_triples_from_capture,
)

from bmd_camera.ble.protocol.codec import encode_assign, encode_assign_void  # noqa: E402
from bmd_camera.ble.protocol.types import DataType  # noqa: E402
from bmd_camera.camera_profile import load_schema, validate_profile  # noqa: E402


def make_candidate(value=2, reserved=1, **overrides) -> CandidateCommand:
    defaults = dict(
        category=0x0A, parameter=0x01, data_type=DataType.INT8, value=value, reserved=reserved
    )
    defaults.update(overrides)
    return CandidateCommand(**defaults)


def make_void_candidate(reserved=0, **overrides) -> CandidateCommand:
    defaults = dict(
        category=0x0A, parameter=0x03, data_type=DataType.VOID, value=None, reserved=reserved
    )
    defaults.update(overrides)
    return CandidateCommand(**defaults)


def make_notification(
    *,
    characteristic_name=INCOMING_CONTROL_NAME,
    category=0x0A,
    parameter=0x01,
    data_type="INT8",
    operation="CAMERA_REPORT",
    payload_hex="02 00 40 00 01 03",
    decode_error=None,
) -> dict:
    """A notification dict in the exact shape save_capture writes."""
    return {
        "timestamp": "2026-07-08T12:00:00.000",
        "characteristic_uuid": "b864e140-76a0-416a-bf30-5876504537d9",
        "characteristic_name": characteristic_name,
        "raw_hex": "FF 0A 00 00 0A 01 01 02 02 00 40 00 01 03",
        "category": category,
        "parameter": parameter,
        "data_type": data_type,
        "operation": operation,
        "payload_hex": payload_hex,
        "decode_error": decode_error,
    }


def make_capture(windows: dict[str, list[dict]]) -> dict:
    """A capture dict in the exact shape save_capture writes."""
    return {
        "model_key": "POCKET_6K_PRO",
        "firmware": "v8.6",
        "captured_at": "2026-07-08T12:00:00",
        "windows": [
            {"label": label, "notifications": notifications, "deduped_triples": []}
            for label, notifications in windows.items()
        ],
    }


class TestCandidateCommand:
    def test_encode_matches_encode_assign(self):
        candidate = make_candidate()

        assert candidate.encode() == encode_assign(
            category=0x0A, parameter=0x01, data_type=DataType.INT8, value=2, reserved=1
        )

    def test_encode_reproduces_known_g2_start_packet(self):
        assert make_candidate(value=2, reserved=1).encode() == bytes(
            [0xFF, 0x05, 0x00, 0x01, 0x0A, 0x01, 0x01, 0x00, 0x02]
        )

    def test_describe_is_hex_formatted(self):
        assert (
            make_candidate().describe() == "category=0x0A parameter=0x01 INT8 value=2 reserved=0x01"
        )

    def test_void_candidate_encodes_via_encode_assign_void(self):
        candidate = make_void_candidate(reserved=1)

        assert candidate.encode() == encode_assign_void(category=0x0A, parameter=0x03, reserved=1)
        assert candidate.encode() == bytes([0xFF, 0x04, 0x00, 0x01, 0x0A, 0x03, 0x00, 0x00])

    def test_void_candidate_describe_says_no_payload(self):
        assert (
            make_void_candidate().describe()
            == "category=0x0A parameter=0x03 VOID void (no payload) reserved=0x00"
        )

    def test_void_candidate_with_a_value_raises(self):
        with pytest.raises(ValueError, match="must be None"):
            make_void_candidate(value=1)

    def test_non_void_candidate_without_a_value_raises(self):
        with pytest.raises(ValueError, match="must be None"):
            make_candidate(value=None)


class TestGenerateCandidates:
    def test_sweep_order_is_reserved_outer_values_inner(self):
        candidates = generate_candidates(
            category=0x0A,
            parameter=0x01,
            data_type=DataType.INT8,
            values=[2, 0],
            reserveds=[1, 0],
        )

        assert [(c.reserved, c.value) for c in candidates] == [(1, 2), (1, 0), (0, 2), (0, 0)]

    def test_operator_ordering_is_preserved(self):
        candidates = generate_candidates(
            category=1, parameter=2, data_type=DataType.INT8, values=[5, 3, 4], reserveds=[0]
        )

        assert [c.value for c in candidates] == [5, 3, 4]

    def test_void_sweep_is_one_candidate_per_reserved(self):
        candidates = generate_candidates(
            category=0x0A, parameter=0x03, data_type=DataType.VOID, values=[], reserveds=[0, 1]
        )

        assert [(c.reserved, c.value) for c in candidates] == [(0, None), (1, None)]

    def test_void_sweep_with_values_raises(self):
        with pytest.raises(ValueError, match="takes no payload values"):
            generate_candidates(
                category=0x0A, parameter=0x03, data_type=DataType.VOID, values=[1], reserveds=[0]
            )


class TestSeedTriplesFromCapture:
    def test_ambient_triples_present_in_every_window_are_dropped(self):
        ambient = make_notification(category=0x09, parameter=0x00, data_type="INT8")
        interesting = make_notification(category=0x0A, parameter=0x01, data_type="INT8")
        capture = make_capture(
            {
                "record_start": [ambient, interesting],
                "record_stop": [ambient],
            }
        )

        assert seed_triples_from_capture(capture) == [(0x0A, 0x01, "INT8")]

    def test_exclude_ambient_false_keeps_everything(self):
        ambient = make_notification(category=0x09, parameter=0x00, data_type="INT8")
        interesting = make_notification(category=0x0A, parameter=0x01, data_type="INT8")
        capture = make_capture({"a": [ambient, interesting], "b": [ambient]})

        triples = seed_triples_from_capture(capture, exclude_ambient=False)

        assert (0x09, 0x00, "INT8") in triples
        assert (0x0A, 0x01, "INT8") in triples

    def test_single_window_capture_keeps_everything(self):
        """The ambient filter needs at least two windows for contrast."""
        ambient = make_notification(category=0x09, parameter=0x00, data_type="INT8")
        capture = make_capture({"only": [ambient]})

        assert seed_triples_from_capture(capture) == [(0x09, 0x00, "INT8")]

    def test_state_report_in_every_window_survives_the_ambient_filter(self):
        """A start/stop family reports the same (category, parameter) in BOTH
        windows by construction, so presence alone would drop the very triple
        the sweep exists to find. One payload per window, differing between
        them, marks it as a state report — see POCKET_6K_G2 v8.6, 2026-07-29."""
        capture = make_capture(
            {
                "record_start": [make_notification(payload_hex="02 00 40 00 01 03")],
                "record_stop": [make_notification(payload_hex="00 00 40 00 01 03")],
            }
        )

        assert seed_triples_from_capture(capture) == [(0x0A, 0x01, "INT8")]

    def test_telemetry_that_varies_within_a_window_is_still_dropped(self):
        """The counterpart: genuine ambient telemetry takes several values
        inside a single window, so it never looks stable-within."""
        capture = make_capture(
            {
                "record_start": [
                    make_notification(category=0x09, parameter=0x00, payload_hex="15 2E 64 00"),
                    make_notification(category=0x09, parameter=0x00, payload_hex="19 2E 64 00"),
                ],
                "record_stop": [
                    make_notification(category=0x09, parameter=0x00, payload_hex="EF 2D 64 00"),
                    make_notification(category=0x09, parameter=0x00, payload_hex="F3 2D 64 00"),
                ],
            }
        )

        assert seed_triples_from_capture(capture) == []

    def test_unchanging_report_in_every_window_is_dropped(self):
        """Stable within *and* identical across windows is not a state report —
        nothing about it tracks the operator's action."""
        capture = make_capture(
            {
                "a": [make_notification(category=0x09, parameter=0x06, payload_hex="00 01")],
                "b": [make_notification(category=0x09, parameter=0x06, payload_hex="00 01")],
            }
        )

        assert seed_triples_from_capture(capture) == []

    def test_real_pocket_6k_g2_v86_recording_capture_surfaces_the_recording_family(self):
        """Replay of the actual 2026-07-29 capture that defeated the old filter.

        Payloads are the real ones from
        tools/captures/POCKET_6K_G2_v8.6/POCKET_6K_G2_v8.6_20260729T121900.json.
        The old heuristic returned [(9,6), (9,2), (12,3)] — three dead ends,
        all confirmed 'nothing observed' on hardware — and dropped (10,1),
        which is the family that was later confirmed."""
        start_window = [
            make_notification(category=0x09, parameter=0x00, payload_hex="15 2E 64 00 0F 00"),
            make_notification(category=0x09, parameter=0x00, payload_hex="19 2E 64 00 0F 00"),
            make_notification(category=0x09, parameter=0x00, payload_hex="16 2E 64 00 0F 00"),
            make_notification(category=0x0A, parameter=0x01, payload_hex="02 00 40 00 01 03"),
            make_notification(category=0x09, parameter=0x06, payload_hex="00 01"),
        ]
        stop_window = [
            make_notification(category=0x09, parameter=0x00, payload_hex="EF 2D 64 00 0F 00"),
            make_notification(category=0x09, parameter=0x00, payload_hex="F3 2D 64 00 0F 00"),
            make_notification(category=0x09, parameter=0x02, payload_hex="00 00 2F 29 00 00 00 00"),
            make_notification(category=0x09, parameter=0x02, payload_hex="00 00 2E 29 00 00 00 00"),
            make_notification(category=0x0A, parameter=0x01, payload_hex="00 00 40 00 01 03"),
            make_notification(category=0x0C, parameter=0x03, payload_hex="03 FF"),
        ]
        capture = make_capture({"record_start": start_window, "record_stop": stop_window})

        triples = seed_triples_from_capture(capture)

        assert (0x0A, 0x01, "INT8") in triples, "the recording family must be seeded"
        assert (0x09, 0x00, "INT8") not in triples, "storage tick is still ambient"

    def test_non_incoming_and_undecoded_notifications_are_ignored(self):
        cam_status = make_notification(
            characteristic_name="CAMERA_STATUS (Read/Notify/Write)",
            category=None,
            parameter=None,
            data_type=None,
            decode_error="too short",
        )
        broken = make_notification(decode_error="Unknown operation byte: 0x05")
        good = make_notification(category=0x0A, parameter=0x01, data_type="INT8")
        capture = make_capture({"a": [cam_status, broken, good], "b": []})

        assert seed_triples_from_capture(capture) == [(0x0A, 0x01, "INT8")]


class TestExtractEcho:
    def test_first_matching_echo_returns_operation_int_and_payload(self):
        notifications = [
            make_notification(category=0x0C, parameter=0x03),
            make_notification(operation="CAMERA_REPORT", payload_hex="02 00 40 00 01 03"),
            make_notification(operation="ASSIGN", payload_hex="FF"),
        ]

        operation, payload = extract_echo(notifications, category=0x0A, parameter=0x01)

        assert operation == 0x02
        assert payload == "02 00 40 00 01 03"

    def test_no_match_returns_none_pair(self):
        notifications = [make_notification(category=0x0C, parameter=0x03)]

        assert extract_echo(notifications, category=0x0A, parameter=0x01) == (None, None)

    def test_undecoded_notifications_are_skipped(self):
        notifications = [make_notification(decode_error="boom")]

        assert extract_echo(notifications, category=0x0A, parameter=0x01) == (None, None)


class TestBuildCommandBlock:
    def make_confirmed(self):
        return [
            ConfirmedOutcome(
                outcome="start",
                candidate=make_candidate(value=2),
                echo_operation=2,
                echo_payload_hex="02 00 40 00 01 03",
            ),
            ConfirmedOutcome(
                outcome="stop",
                candidate=make_candidate(value=0),
                echo_operation=2,
                echo_payload_hex="00 00 40 00 01 03",
            ),
        ]

    def test_block_validates_against_real_schema(self):
        """Strong coupling test: an emitted block, dropped into a minimal
        profile, must pass the same validation the loader runs."""
        block = build_command_block(
            name="recording",
            confirmed=self.make_confirmed(),
            capture_ref="tools/captures/POCKET_6K_PRO_v8.6/x.json",
            discovered_on="2026-07-08",
        )

        profile = {
            "_meta": {
                "model": "Pocket Cinema Camera 6K Pro",
                "model_key": "POCKET_6K_PRO",
                "firmware": "v8.6",
                "ble_name": "A:026881AD",
                "status": "UNVERIFIED",
            },
            "commands": {"recording": block},
        }
        validate_profile(profile, source="emitted.json")

        assert block["category"] == 0x0A
        assert block["values"] == {"start": 2, "stop": 0}
        assert block["echo_operation"] == 2
        assert block["provenance"]["status"] == "VERIFIED"
        assert block["provenance"]["capture_refs"] == ["tools/captures/POCKET_6K_PRO_v8.6/x.json"]

    def test_block_also_validates_via_command_defs_directly(self):
        block = build_command_block(
            name="recording",
            confirmed=self.make_confirmed(),
            capture_ref=None,
            discovered_on="2026-07-08",
        )
        schema = load_schema()

        jsonschema.validate(block, {"$ref": "#/$defs/command", "$defs": schema["$defs"]})

    def test_echo_operation_omitted_when_no_echo_decoded(self):
        confirmed = [
            ConfirmedOutcome(outcome="start", candidate=make_candidate(value=2)),
            ConfirmedOutcome(outcome="stop", candidate=make_candidate(value=0)),
        ]

        block = build_command_block(
            name="recording", confirmed=confirmed, capture_ref=None, discovered_on="2026-07-08"
        )

        assert "echo_operation" not in block

    def test_raises_on_empty_confirmations(self):
        with pytest.raises(ValueError, match="No confirmed outcomes"):
            build_command_block(
                name="recording", confirmed=[], capture_ref=None, discovered_on="2026-07-08"
            )

    def test_raises_when_coordinates_disagree(self):
        confirmed = [
            ConfirmedOutcome(outcome="start", candidate=make_candidate(value=2, reserved=1)),
            ConfirmedOutcome(outcome="stop", candidate=make_candidate(value=0, reserved=0)),
        ]

        with pytest.raises(ValueError, match="disagree on command coordinates"):
            build_command_block(
                name="recording", confirmed=confirmed, capture_ref=None, discovered_on="2026-07-08"
            )

    def test_raises_when_two_outcomes_share_a_value(self):
        confirmed = [
            ConfirmedOutcome(outcome="start", candidate=make_candidate(value=2)),
            ConfirmedOutcome(outcome="stop", candidate=make_candidate(value=2)),
        ]

        with pytest.raises(ValueError, match="confirmed for two outcomes"):
            build_command_block(
                name="recording", confirmed=confirmed, capture_ref=None, discovered_on="2026-07-08"
            )

    def test_raises_when_outcome_confirmed_twice(self):
        confirmed = [
            ConfirmedOutcome(outcome="start", candidate=make_candidate(value=2)),
            ConfirmedOutcome(outcome="start", candidate=make_candidate(value=0)),
        ]

        with pytest.raises(ValueError, match="more than once"):
            build_command_block(
                name="recording", confirmed=confirmed, capture_ref=None, discovered_on="2026-07-08"
            )

    def test_void_block_omits_values_and_validates_against_real_schema(self):
        confirmed = [
            ConfirmedOutcome(
                outcome="photo_taken", candidate=make_void_candidate(reserved=1), echo_operation=2
            )
        ]

        block = build_command_block(
            name="photo", confirmed=confirmed, capture_ref=None, discovered_on="2026-07-27"
        )
        schema = load_schema()
        jsonschema.validate(block, {"$ref": "#/$defs/command", "$defs": schema["$defs"]})

        assert "values" not in block
        assert block["data_type"] == "VOID"
        assert block["reserved"] == 1
        assert block["echo_operation"] == 2
        assert "photo_taken" in block["provenance"]["notes"]

    def test_void_block_still_raises_on_duplicate_outcome(self):
        confirmed = [
            ConfirmedOutcome(outcome="photo_taken", candidate=make_void_candidate(reserved=0)),
            ConfirmedOutcome(outcome="photo_taken", candidate=make_void_candidate(reserved=0)),
        ]

        with pytest.raises(ValueError, match="more than once"):
            build_command_block(
                name="photo", confirmed=confirmed, capture_ref=None, discovered_on="2026-07-27"
            )


class TestRenderProfileSnippet:
    def test_snippet_round_trips_through_json(self):
        block = build_command_block(
            name="recording",
            confirmed=[
                ConfirmedOutcome(outcome="start", candidate=make_candidate(value=2)),
                ConfirmedOutcome(outcome="stop", candidate=make_candidate(value=0)),
            ],
            capture_ref=None,
            discovered_on="2026-07-08",
        )

        snippet = render_profile_snippet("recording", block)

        assert json.loads(snippet) == {"recording": block}
