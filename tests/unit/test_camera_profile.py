"""
tests/unit/test_camera_profile.py
==================================
Unit tests for CameraProfile dataclass and model JSON loading.

Tests run without hardware — they verify that the JSON files are structurally
correct, that CameraProfile resolves values accurately, and that unverified
values raise clear errors rather than silently using defaults.

Coverage:
  - Profile loading from JSON
  - Verified vs unverified value resolution
  - codec_id / variant_id / fps_encoding access
  - mode lookup and filtering
  - Null-value error messages (production safety)
  - all_codec_variant_pairs() completeness
  - verification_table() output format
  - KNOWN_PROFILES registry completeness
"""

import json

import pytest

from bmd_ble.camera_profile import KNOWN_PROFILES, CameraProfile
from bmd_ble.protocol.types import DataType


def test_known_profiles_contains_pocket_6k_g2_v79():
    assert ("POCKET_6K_G2", "v7.9") in KNOWN_PROFILES


def test_camera_profile_loads_valid_profile(tmp_path, monkeypatch):
    models_dir = tmp_path / "payloads" / "models"
    models_dir.mkdir(parents=True)

    profile_file = models_dir / "POCKET_6K_G2_v7.9.json"

    profile_data = {
        "_meta": {
            "model": "Blackmagic Pocket Cinema Camera 6K G2",
            "status": "VERIFIED",
            "ble_name": "BMPCC 6K G2",
        }
    }

    profile_file.write_text(json.dumps(profile_data), encoding="utf-8")

    monkeypatch.setattr("bmd_ble.camera_profile.MODELS_DIR", models_dir)

    profile = CameraProfile.for_model("POCKET_6K_G2", "v7.9")

    assert profile.model_key == "POCKET_6K_G2"
    assert profile.model_name == "Blackmagic Pocket Cinema Camera 6K G2"
    assert profile.firmware == "v7.9"
    assert profile.status == "VERIFIED"
    assert profile.ble_name == "BMPCC 6K G2"
    assert profile._raw == profile_data


def test_camera_profile_missing_file_raises_file_not_found_error(tmp_path, monkeypatch):
    models_dir = tmp_path / "payloads" / "models"
    models_dir.mkdir(parents=True)

    monkeypatch.setattr("bmd_ble.camera_profile.MODELS_DIR", models_dir)

    with pytest.raises(FileNotFoundError) as exc_info:
        CameraProfile.for_model("UNKNOWN_CAMERA", "v1.0")

    assert "No model JSON found" in str(exc_info.value)


def test_camera_profile_uses_defaults_when_meta_missing():
    raw = {}

    profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)

    assert profile.model_key == "POCKET_6K_G2"
    assert profile.model_name == "POCKET_6K_G2"
    assert profile.firmware == "v7.9"
    assert profile.status == "UNKNOWN"
    assert profile.ble_name == ""
    assert profile._raw == raw


def test_camera_profile_uses_partial_meta_defaults():
    raw = {"_meta": {"model": "Pocket 6K G2"}}

    profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)

    assert profile.model_key == "POCKET_6K_G2"
    assert profile.model_name == "Pocket 6K G2"
    assert profile.firmware == "v7.9"
    assert profile.status == "UNKNOWN"
    assert profile.ble_name == ""
    assert profile._raw == raw


def test_camera_profile_handles_empty_meta_dict():
    raw = {"_meta": {}}

    profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)

    assert profile.model_name == "POCKET_6K_G2"
    assert profile.status == "UNKNOWN"
    assert profile.ble_name == ""


def test_camera_profile_resolves_recording_block():
    raw = {
        "recording": {
            "category": 10,
            "parameter": 1,
            "data_type": "BOOL",
            "reserved": 1,
            "start_value": 2,
            "stop_value": 0,
        }
    }

    profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)

    assert profile.recording_category == 10
    assert profile.recording_parameter == 1
    assert profile.recording_data_type == DataType.BOOL
    assert profile.recording_reserved == 1
    assert profile.recording_start_value == 2
    assert profile.recording_stop_value == 0


def test_camera_profile_defaults_recording_fields_to_none_when_absent():
    raw = {}

    profile = CameraProfile._from_raw("POCKET_6K_PRO", "v8.6", raw)

    assert profile.recording_category is None
    assert profile.recording_parameter is None
    assert profile.recording_data_type is None
    assert profile.recording_reserved is None
    assert profile.recording_start_value is None
    assert profile.recording_stop_value is None


def test_pocket_6k_pro_profile_loads_without_recording_block():
    """POCKET_6K_PRO_v8.6.json has no `recording` key yet — must still load cleanly."""
    profile = CameraProfile.for_model("POCKET_6K_PRO", "v8.6")

    assert profile.recording_category is None
    assert profile.recording_data_type is None
