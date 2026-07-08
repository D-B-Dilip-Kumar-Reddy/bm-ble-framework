"""
tests/unit/test_camera_profile.py
==================================
Unit tests for CameraProfile dataclass, schema validation, and model JSON
loading.

Tests run without hardware — they verify that the real profile JSONs conform
to payloads/schema.json, that CameraProfile resolves command blocks into
CommandSpec accurately, and that malformed profiles raise clear errors at
load time rather than silently using defaults.

Coverage:
  - Every KNOWN_PROFILES JSON validates against the real schema and loads
  - commands.* resolution into CommandSpec / CommandProvenance
  - require_command error messages (production safety)
  - validate_profile rejections (typos, bad enums, wrong types)
  - filename ↔ _meta identity cross-check
  - UNVERIFIED warning at load time (design principle 8)
  - _from_raw leniency for partial dicts (unit-test escape hatch)
"""

import json
import logging

import pytest

from bmd_ble.camera_profile import (
    KNOWN_PROFILES,
    CameraProfile,
    CommandSpec,
    validate_profile,
)
from bmd_ble.protocol.types import DataType


def make_valid_raw(**overrides) -> dict:
    """A minimal schema-valid profile dict."""
    raw = {
        "_meta": {
            "model": "Pocket Cinema Camera 6K G2",
            "model_key": "POCKET_6K_G2",
            "firmware": "v7.9",
            "ble_name": "A:AF3DC814",
            "status": "UNVERIFIED",
        },
        "commands": {
            "recording": {
                "category": 10,
                "parameter": 1,
                "data_type": "BOOL",
                "reserved": 1,
                "values": {"start": 2, "stop": 0},
                "echo_operation": 2,
                "provenance": {"status": "VERIFIED"},
            }
        },
    }
    raw.update(overrides)
    return raw


class TestKnownProfiles:
    def test_known_profiles_contains_pocket_6k_g2_v79(self):
        assert ("POCKET_6K_G2", "v7.9") in KNOWN_PROFILES

    def test_known_profiles_contains_pocket_6k_pro_v86(self):
        assert ("POCKET_6K_PRO", "v8.6") in KNOWN_PROFILES

    @pytest.mark.parametrize(("model_key", "firmware"), KNOWN_PROFILES)
    def test_every_known_profile_validates_and_loads(self, model_key, firmware):
        """Regression net: each real payloads/models JSON passes schema
        validation and loads into a CameraProfile."""
        profile = CameraProfile.for_model(model_key, firmware)

        assert profile.model_key == model_key
        assert profile.firmware == firmware
        assert profile.ble_name  # real advertisement name, never empty


class TestProfileLoading:
    def test_loads_valid_profile(self, tmp_path, monkeypatch):
        models_dir = tmp_path / "models"
        models_dir.mkdir(parents=True)
        profile_data = make_valid_raw()
        (models_dir / "POCKET_6K_G2_v7.9.json").write_text(
            json.dumps(profile_data), encoding="utf-8"
        )
        monkeypatch.setattr("bmd_ble.camera_profile.MODELS_DIR", models_dir)

        profile = CameraProfile.for_model("POCKET_6K_G2", "v7.9")

        assert profile.model_key == "POCKET_6K_G2"
        assert profile.model_name == "Pocket Cinema Camera 6K G2"
        assert profile.firmware == "v7.9"
        assert profile.status == "UNVERIFIED"
        assert profile.ble_name == "A:AF3DC814"
        assert profile._raw == profile_data

    def test_missing_file_raises_file_not_found_error(self, tmp_path, monkeypatch):
        models_dir = tmp_path / "models"
        models_dir.mkdir(parents=True)
        monkeypatch.setattr("bmd_ble.camera_profile.MODELS_DIR", models_dir)

        with pytest.raises(FileNotFoundError, match="No model JSON found"):
            CameraProfile.for_model("UNKNOWN_CAMERA", "v1.0")

    def test_meta_identity_mismatch_raises(self, tmp_path, monkeypatch):
        models_dir = tmp_path / "models"
        models_dir.mkdir(parents=True)
        raw = make_valid_raw()
        # File named as PRO v8.6, but _meta declares the G2 v7.9.
        (models_dir / "POCKET_6K_PRO_v8.6.json").write_text(json.dumps(raw), encoding="utf-8")
        monkeypatch.setattr("bmd_ble.camera_profile.MODELS_DIR", models_dir)

        with pytest.raises(ValueError, match="_meta.model_key"):
            CameraProfile.for_model("POCKET_6K_PRO", "v8.6")

    def test_unverified_profile_logs_warning(self, tmp_path, monkeypatch, caplog):
        models_dir = tmp_path / "models"
        models_dir.mkdir(parents=True)
        (models_dir / "POCKET_6K_G2_v7.9.json").write_text(
            json.dumps(make_valid_raw()), encoding="utf-8"
        )
        monkeypatch.setattr("bmd_ble.camera_profile.MODELS_DIR", models_dir)

        with caplog.at_level(logging.WARNING, logger="bmd_ble.camera_profile"):
            CameraProfile.for_model("POCKET_6K_G2", "v7.9")

        assert any("UNVERIFIED" in record.message for record in caplog.records)


class TestValidateProfile:
    def test_accepts_valid_profile(self):
        validate_profile(make_valid_raw(), source="test.json")

    def test_rejects_missing_meta_field(self):
        raw = make_valid_raw()
        del raw["_meta"]["ble_name"]
        with pytest.raises(ValueError, match="ble_name"):
            validate_profile(raw, source="test.json")

    def test_rejects_bad_status_enum(self):
        raw = make_valid_raw()
        raw["_meta"]["status"] = "MOSTLY_VERIFIED"
        with pytest.raises(ValueError, match="status"):
            validate_profile(raw, source="test.json")

    def test_rejects_unknown_key_in_command_block(self):
        """additionalProperties: false catches typos like start_val."""
        raw = make_valid_raw()
        raw["commands"]["recording"]["start_val"] = 2
        with pytest.raises(ValueError, match="start_val"):
            validate_profile(raw, source="test.json")

    def test_rejects_non_integer_command_value(self):
        raw = make_valid_raw()
        raw["commands"]["recording"]["values"]["start"] = "2"
        with pytest.raises(ValueError, match="values"):
            validate_profile(raw, source="test.json")

    def test_rejects_unknown_data_type_name(self):
        raw = make_valid_raw()
        raw["commands"]["recording"]["data_type"] = "UINT8"
        with pytest.raises(ValueError, match="data_type"):
            validate_profile(raw, source="test.json")

    def test_rejects_command_without_provenance(self):
        raw = make_valid_raw()
        del raw["commands"]["recording"]["provenance"]
        with pytest.raises(ValueError, match="provenance"):
            validate_profile(raw, source="test.json")

    def test_error_message_names_source_file(self):
        raw = make_valid_raw()
        raw["_meta"]["status"] = "BOGUS"
        with pytest.raises(ValueError, match="test.json"):
            validate_profile(raw, source="test.json")

    def test_allows_comment_keys_anywhere(self):
        raw = make_valid_raw()
        raw["_comment"] = "top-level note"
        raw["commands"]["_comment"] = "commands note"
        raw["commands"]["recording"]["_comment"] = "block note"
        validate_profile(raw, source="test.json")


class TestFromRawLeniency:
    """_from_raw stays deliberately lenient (validation is for_model's job)
    so unit tests can build partial profiles."""

    def test_uses_defaults_when_meta_missing(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", {})

        assert profile.model_key == "POCKET_6K_G2"
        assert profile.model_name == "POCKET_6K_G2"
        assert profile.firmware == "v7.9"
        assert profile.status == "UNKNOWN"
        assert profile.ble_name == ""
        assert profile.commands == {}

    def test_uses_partial_meta_defaults(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", {"_meta": {"model": "P6K G2"}})

        assert profile.model_name == "P6K G2"
        assert profile.status == "UNKNOWN"


class TestCommandResolution:
    def test_resolves_recording_command_block(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", make_valid_raw())

        spec = profile.command("recording")
        assert isinstance(spec, CommandSpec)
        assert spec.name == "recording"
        assert spec.category == 10
        assert spec.parameter == 1
        assert spec.data_type == DataType.BOOL
        assert spec.reserved == 1
        assert spec.values == {"start": 2, "stop": 0}
        assert spec.echo_operation == 2
        assert spec.provenance is not None
        assert spec.provenance.status == "VERIFIED"

    def test_reserved_defaults_to_zero_when_absent(self):
        raw = make_valid_raw()
        del raw["commands"]["recording"]["reserved"]
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)

        assert profile.command("recording").reserved == 0x00

    def test_comment_keys_are_skipped(self):
        raw = make_valid_raw()
        raw["commands"]["_comment"] = "note"
        raw["commands"]["recording"]["values"]["_comment"] = "note"
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)

        assert set(profile.commands) == {"recording"}
        assert profile.command("recording").values == {"start": 2, "stop": 0}

    def test_command_returns_none_when_absent(self):
        profile = CameraProfile._from_raw("POCKET_6K_PRO", "v8.6", {})

        assert profile.command("recording") is None


class TestRequireCommand:
    def test_returns_spec_when_present(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", make_valid_raw())

        spec = profile.require_command("recording", ("start", "stop"))
        assert spec.values["start"] == 2

    def test_raises_naming_missing_block(self):
        profile = CameraProfile._from_raw("POCKET_6K_PRO", "v8.6", {})

        with pytest.raises(ValueError, match="no 'recording' command block"):
            profile.require_command("recording")

    def test_raises_naming_missing_values(self):
        raw = make_valid_raw()
        del raw["commands"]["recording"]["values"]["stop"]
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)

        with pytest.raises(ValueError, match="missing.*values: stop"):
            profile.require_command("recording", ("start", "stop"))


def test_pocket_6k_pro_profile_resolves_recording_command():
    """POCKET_6K_PRO_v8.6.json's recording block was populated via
    tools/control/discover_command.py on real hardware — must load and
    resolve identically to the G2's (same category/parameter/values)."""
    profile = CameraProfile.for_model("POCKET_6K_PRO", "v8.6")

    spec = profile.require_command("recording", ("start", "stop"))
    assert spec.category == 10
    assert spec.parameter == 1
    assert spec.values == {"start": 2, "stop": 0}
    assert spec.provenance is not None
    assert spec.provenance.status == "VERIFIED"
    assert spec.provenance.method == "guided-discovery (tools/control/discover_command.py)"
