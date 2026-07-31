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
    StorageSignalSpec,
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
                "data_type": "INT8",
                "reserved": 1,
                "values": {"start": 2, "stop": 0},
                "echo_operation": 2,
                "provenance": {"status": "VERIFIED"},
            }
        },
    }
    raw.update(overrides)
    return raw


def make_valid_raw_with_storage(**overrides) -> dict:
    """`make_valid_raw` plus a schema-valid `storage.write_margin_warning` block."""
    raw = make_valid_raw(
        storage={
            "write_margin_warning": {
                "category": 9,
                "parameter": 1,
                "data_type": "INT8",
                "byte_offset": 1,
                "values": {"nominal": 1, "low_margin": -2},
                "provenance": {"status": "CANDIDATE"},
            }
        }
    )
    raw.update(overrides)
    return raw


class TestKnownProfiles:
    def test_known_profiles_contains_pocket_6k_g2_v79(self):
        assert ("POCKET_6K_G2", "v7.9") in KNOWN_PROFILES

    def test_known_profiles_contains_pocket_6k_g2_v86(self):
        assert ("POCKET_6K_G2", "v8.6") in KNOWN_PROFILES

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

    def test_non_verified_storage_signal_logs_info(self, tmp_path, monkeypatch, caplog):
        """A CANDIDATE storage entry gets the same provenance-status INFO
        log every non-VERIFIED commands block already gets — the logging
        loop must walk storage too, not just commands."""
        models_dir = tmp_path / "models"
        models_dir.mkdir(parents=True)
        (models_dir / "POCKET_6K_G2_v7.9.json").write_text(
            json.dumps(make_valid_raw_with_storage()), encoding="utf-8"
        )
        monkeypatch.setattr("bmd_ble.camera_profile.MODELS_DIR", models_dir)

        with caplog.at_level(logging.INFO, logger="bmd_ble.camera_profile"):
            CameraProfile.for_model("POCKET_6K_G2", "v7.9")

        assert any(
            "write_margin_warning" in record.message and "CANDIDATE" in record.message
            for record in caplog.records
        )


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

    def test_accepts_valid_storage_block(self):
        validate_profile(make_valid_raw_with_storage(), source="test.json")

    def test_rejects_unknown_key_in_storage_block(self):
        """additionalProperties: false catches typos, same as commands."""
        raw = make_valid_raw_with_storage()
        raw["storage"]["write_margin_warning"]["catagory"] = 9
        with pytest.raises(ValueError, match="catagory"):
            validate_profile(raw, source="test.json")

    def test_rejects_non_integer_storage_value(self):
        raw = make_valid_raw_with_storage()
        raw["storage"]["write_margin_warning"]["values"]["nominal"] = "1"
        with pytest.raises(ValueError, match="values"):
            validate_profile(raw, source="test.json")

    def test_rejects_storage_block_without_provenance(self):
        raw = make_valid_raw_with_storage()
        del raw["storage"]["write_margin_warning"]["provenance"]
        with pytest.raises(ValueError, match="provenance"):
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
        assert profile.storage == {}

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
        assert spec.data_type == DataType.INT8
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


class TestStorageResolution:
    def test_resolves_write_margin_warning_block(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", make_valid_raw_with_storage())

        spec = profile.storage_signal("write_margin_warning")
        assert isinstance(spec, StorageSignalSpec)
        assert spec.name == "write_margin_warning"
        assert spec.category == 9
        assert spec.parameter == 1
        assert spec.data_type == DataType.INT8
        assert spec.byte_offset == 1
        assert spec.values == {"nominal": 1, "low_margin": -2}
        assert spec.provenance is not None
        assert spec.provenance.status == "CANDIDATE"

    def test_byte_offset_defaults_to_zero_when_absent(self):
        raw = make_valid_raw_with_storage()
        del raw["storage"]["write_margin_warning"]["byte_offset"]
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)

        assert profile.storage_signal("write_margin_warning").byte_offset == 0

    def test_comment_keys_are_skipped(self):
        raw = make_valid_raw_with_storage()
        raw["storage"]["_comment"] = "note"
        raw["storage"]["write_margin_warning"]["values"]["_comment"] = "note"
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)

        assert set(profile.storage) == {"write_margin_warning"}
        assert profile.storage_signal("write_margin_warning").values == {
            "nominal": 1,
            "low_margin": -2,
        }

    def test_storage_signal_returns_none_when_absent(self):
        profile = CameraProfile._from_raw("POCKET_6K_PRO", "v8.6", {})

        assert profile.storage_signal("write_margin_warning") is None


class TestRequireStorageSignal:
    def test_returns_spec_when_present(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", make_valid_raw_with_storage())

        spec = profile.require_storage_signal("write_margin_warning", ("nominal", "low_margin"))
        assert spec.values["low_margin"] == -2

    def test_raises_naming_missing_block(self):
        profile = CameraProfile._from_raw("POCKET_6K_PRO", "v8.6", {})

        with pytest.raises(ValueError, match="no 'write_margin_warning' storage block"):
            profile.require_storage_signal("write_margin_warning")

    def test_raises_naming_missing_values(self):
        raw = make_valid_raw_with_storage()
        del raw["storage"]["write_margin_warning"]["values"]["low_margin"]
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)

        with pytest.raises(ValueError, match="missing.*values: low_margin"):
            profile.require_storage_signal("write_margin_warning", ("nominal", "low_margin"))


def make_valid_raw_with_settings(**overrides) -> dict:
    """`make_valid_raw` plus schema-valid settings command blocks and the
    codecs/resolutions/fps_modes lookup tables they consume."""
    raw = make_valid_raw(
        codecs={
            "BRAW": {"id": 3, "variants": {"Q0": 0, "5:1": 3}},
            "ProRes": {"id": 2, "variants": {"HQ": 0}},
        },
        resolutions={
            "4K DCI": {
                "width": 4096,
                "height": 2160,
                "codecs": ["BRAW", "ProRes"],
                "dimension_enums": {"BRAW": 8},
            }
        },
        fps_modes={"25": {"fps_int": 25, "m_rate": 0, "frame_flags": 16}},
    )
    raw["commands"]["codec_quality"] = {
        "category": 10,
        "parameter": 0,
        "data_type": "INT8",
        "reserved": 0,
        "provenance": {"status": "CANDIDATE"},
    }
    raw.update(overrides)
    return raw


class TestSettingsSections:
    def test_command_block_without_values_resolves_to_empty_map(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", make_valid_raw_with_settings())

        spec = profile.require_command("codec_quality")
        assert spec.values == {}
        assert spec.reserved == 0

    def test_schema_accepts_command_block_without_values(self):
        validate_profile(make_valid_raw_with_settings(), source="test.json")

    def test_resolves_codec_spec(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", make_valid_raw_with_settings())

        codec = profile.require_codec("BRAW", "5:1")
        assert codec.id == 3
        assert codec.variants["5:1"] == 3

    def test_resolves_resolution_spec(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", make_valid_raw_with_settings())

        resolution = profile.require_resolution("4K DCI")
        assert (resolution.width, resolution.height) == (4096, 2160)
        assert resolution.codecs == ("BRAW", "ProRes")
        assert resolution.dimension_enums == {"BRAW": 8}
        assert resolution.known_unreachable == {}
        assert resolution.max_fps_int is None

    def test_resolves_resolution_max_fps_int(self):
        raw = make_valid_raw_with_settings()
        raw["resolutions"]["4K DCI"]["max_fps_int"] = 50
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)

        resolution = profile.require_resolution("4K DCI")
        assert resolution.max_fps_int == 50

    def test_resolves_resolution_known_unreachable(self):
        raw = make_valid_raw_with_settings()
        raw["resolutions"]["4K DCI"]["known_unreachable"] = {"ProRes": "evidence note"}
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)

        resolution = profile.require_resolution("4K DCI")
        assert resolution.known_unreachable == {"ProRes": "evidence note"}

    def test_known_unreachable_comment_keys_are_skipped(self):
        raw = make_valid_raw_with_settings()
        raw["resolutions"]["4K DCI"]["known_unreachable"] = {
            "ProRes": "evidence note",
            "_comment": "skip me",
        }
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)

        resolution = profile.require_resolution("4K DCI")
        assert resolution.known_unreachable == {"ProRes": "evidence note"}

    def test_resolves_fps_mode_spec(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", make_valid_raw_with_settings())

        fps = profile.require_fps_mode("25")
        assert (fps.fps_int, fps.m_rate, fps.frame_flags) == (25, 0, 16)

    def test_require_codec_raises_naming_known_codecs(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", make_valid_raw_with_settings())

        with pytest.raises(ValueError, match="no codec 'H265'.*BRAW, ProRes"):
            profile.require_codec("H265")

    def test_require_codec_raises_naming_known_variants(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", make_valid_raw_with_settings())

        with pytest.raises(ValueError, match="no variant '12:1'"):
            profile.require_codec("BRAW", "12:1")

    def test_require_resolution_raises_naming_known_resolutions(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", make_valid_raw_with_settings())

        with pytest.raises(ValueError, match="no resolution 'UHD'.*4K DCI"):
            profile.require_resolution("UHD")

    def test_require_fps_mode_raises_naming_known_modes(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", make_valid_raw_with_settings())

        with pytest.raises(ValueError, match="no fps mode '23.98'.*25"):
            profile.require_fps_mode("23.98")

    def test_comment_keys_are_skipped_in_lookup_tables(self):
        raw = make_valid_raw_with_settings()
        raw["codecs"]["_comment"] = "skip me"
        raw["resolutions"]["_comment"] = "skip me"
        raw["fps_modes"]["_comment"] = "skip me"
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)

        assert set(profile.codecs) == {"BRAW", "ProRes"}
        assert set(profile.resolutions) == {"4K DCI"}
        assert set(profile.fps_modes) == {"25"}

    def test_schema_rejects_codec_without_id(self):
        raw = make_valid_raw_with_settings()
        del raw["codecs"]["BRAW"]["id"]

        with pytest.raises(ValueError, match="schema validation"):
            validate_profile(raw, source="test.json")

    def test_schema_rejects_resolution_with_unknown_key(self):
        raw = make_valid_raw_with_settings()
        raw["resolutions"]["4K DCI"]["dimension_enum"] = 8  # not the plural key

        with pytest.raises(ValueError, match="schema validation"):
            validate_profile(raw, source="test.json")

    def test_schema_accepts_resolution_known_unreachable(self):
        raw = make_valid_raw_with_settings()
        raw["resolutions"]["4K DCI"]["known_unreachable"] = {"ProRes": "evidence note"}

        validate_profile(raw, source="test.json")

    def test_schema_rejects_known_unreachable_with_non_string_value(self):
        raw = make_valid_raw_with_settings()
        raw["resolutions"]["4K DCI"]["known_unreachable"] = {"ProRes": 123}

        with pytest.raises(ValueError, match="schema validation"):
            validate_profile(raw, source="test.json")

    def test_schema_accepts_resolution_max_fps_int(self):
        raw = make_valid_raw_with_settings()
        raw["resolutions"]["4K DCI"]["max_fps_int"] = 50

        validate_profile(raw, source="test.json")

    def test_schema_rejects_max_fps_int_below_minimum(self):
        raw = make_valid_raw_with_settings()
        raw["resolutions"]["4K DCI"]["max_fps_int"] = 0

        with pytest.raises(ValueError, match="schema validation"):
            validate_profile(raw, source="test.json")

    def test_schema_rejects_bad_m_rate(self):
        raw = make_valid_raw_with_settings()
        raw["fps_modes"]["25"]["m_rate"] = 2

        with pytest.raises(ValueError, match="schema validation"):
            validate_profile(raw, source="test.json")

    def test_schema_accepts_int16_array_data_type(self):
        raw = make_valid_raw_with_settings()
        raw["commands"]["recording_format"] = {
            "category": 1,
            "parameter": 9,
            "data_type": "INT16_ARRAY",
            "reserved": 1,
            "provenance": {"status": "CANDIDATE"},
        }

        validate_profile(raw, source="test.json")
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", raw)
        assert profile.require_command("recording_format").data_type is DataType.INT16_ARRAY


def test_pocket_6k_g2_profile_resolves_settings_blocks():
    """POCKET_6K_G2_v7.9.json's settings families, originally transcribed
    from an external reverse-engineering doc — see docs/settings.md. Spot-
    check the load path end to end; all three are now VERIFIED on real
    hardware (docs/settings.md §8/§10)."""
    profile = CameraProfile.for_model("POCKET_6K_G2", "v7.9")

    codec_quality = profile.require_command("codec_quality")
    assert (codec_quality.category, codec_quality.parameter) == (10, 0)
    assert codec_quality.reserved == 0
    # Observed on the 2026-07-20 passive capture — camera reports use
    # CAMERA_REPORT (0x02) on this family's coordinates.
    assert codec_quality.echo_operation == 2
    # Promoted 2026-07-20: CameraSession.set_codec_quality() confirmed a
    # genuine (non-redundant) real-hardware write+echo cycle via
    # examples/change_codec.py's set_camera_format().
    assert codec_quality.provenance.status == "VERIFIED"

    video_format = profile.require_command("video_format")
    assert (video_format.category, video_format.parameter) == (1, 0)
    assert video_format.reserved == 1
    # The camera never reports on 1/0 (2026-07-20 capture) — no
    # echo_operation may be recorded until a write-side echo is observed.
    assert video_format.echo_operation is None
    # Promoted 2026-07-20: CameraSession.set_video_format() itself confirmed
    # 2/2 real-hardware switches via examples/change_codec.py, on top of the
    # dimension_enum probe sweep's 8/8 byte-exact confirmations.
    assert video_format.provenance.status == "VERIFIED"

    recording_format = profile.require_command("recording_format")
    assert (recording_format.category, recording_format.parameter) == (1, 9)
    assert recording_format.data_type is DataType.INT16_ARRAY
    assert recording_format.echo_operation == 2
    # Promoted 2026-07-20: CameraSession.set_recording_format() confirmed a
    # genuine write+echo cycle (including the CANDIDATE 0x82 data-type byte)
    # via examples/change_codec.py's set_camera_format().
    assert recording_format.provenance.status == "VERIFIED"

    assert profile.require_codec("BRAW", "5:1").id == 3
    assert profile.require_codec("ProRes", "HQ").id == 2
    four_k = profile.require_resolution("4K DCI")
    # ProRes enum still unknown — 0x01-0x16 all probed 2026-07-20, none matched.
    assert four_k.dimension_enums == {"BRAW": 8}
    assert profile.require_fps_mode("23.98").m_rate == 1
    assert profile.require_fps_mode("23.98").fps_int == 24


def test_pocket_6k_pro_profile_resolves_settings_blocks():
    """POCKET_6K_PRO_v8.6.json's settings blocks/tables were populated from
    this camera's own real captures (2026-07-21) — CLAUDE.md design
    principle 6 requires that, never copied from the G2. All three command
    blocks and the codecs/resolutions tables stay CANDIDATE (not VERIFIED)
    until a full CameraSession write+echo round trip is attempted — see
    docs/settings.md's PRO section."""
    profile = CameraProfile.for_model("POCKET_6K_PRO", "v8.6")

    for name in ("video_format", "codec_quality", "recording_format"):
        spec = profile.require_command(name)
        assert spec.provenance.status == "CANDIDATE"

    codec_quality = profile.require_command("codec_quality")
    assert (codec_quality.category, codec_quality.parameter) == (10, 0)
    recording_format = profile.require_command("recording_format")
    assert (recording_format.category, recording_format.parameter) == (1, 9)

    assert profile.require_codec("BRAW", "5:1").id == 3
    assert profile.require_codec("ProRes", "HQ").id == 2

    four_k = profile.require_resolution("4K DCI")
    # ProRes enum unknown here too — same open gap as the G2's 4K DCI/ProRes.
    assert four_k.dimension_enums == {"BRAW": 8}
    six_k = profile.require_resolution("6K")
    assert (six_k.width, six_k.height) == (6144, 3456)
    assert six_k.dimension_enums == {"BRAW": 19}

    assert profile.require_fps_mode("50").fps_int == 50


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


def test_pocket_6k_g2_v86_profile_resolves_recording_command():
    """POCKET_6K_G2_v8.6.json's recording block, discovered on real hardware
    2026-07-29. Same coordinates and values as v7.9, but ``reserved`` is 0
    where v7.9 uses 1 — the camera accepted both, and 0 is the one with a
    clean wire echo for both outcomes (see docs/recording.md). The assertion
    below is the regression net for that difference: it must not silently
    drift back to the v7.9 value."""
    profile = CameraProfile.for_model("POCKET_6K_G2", "v8.6")

    spec = profile.require_command("recording", ("start", "stop"))
    assert (spec.category, spec.parameter) == (10, 1)
    assert spec.data_type is DataType.INT8
    assert spec.values == {"start": 2, "stop": 0}
    assert spec.reserved == 0
    assert spec.echo_operation == 2
    assert spec.provenance is not None
    # Promoted 2026-07-29 on Phase 2 step 8.5: examples/record_start_stop.py
    # confirmed 3/3 start and 3/3 stop by echo through the real CameraSession.
    assert spec.provenance.status == "VERIFIED"


def test_pocket_6k_g2_v86_profile_resolves_fps_modes():
    """POCKET_6K_G2_v8.6.json's fps_modes, sniffed 2026-07-30 across step 9's
    combined sweep, a dedicated follow-up fps sweep, and three standalone
    fps_60 retries (docs/settings.md §18/§18.7/§18.8). All 8 standard rates.

    ``60`` initially looked like a candidate hardware ceiling — two separate
    sweeps produced no 0x01/0x09 report for it — but that finding was
    RETRACTED: three follow-up standalone attempts reported it cleanly twice,
    byte-identical to each other and matching the exact-rate flags pattern
    (§18.8). The camera demonstrably reaches and reports this state, so it
    belongs in this table like every other confirmed rate; the remaining
    silent attempts are an open report-observability question, not a
    capability finding, and must not be read as evidence of a ceiling.

    frame_flags follows the windowed-sensor pattern confirmed in this
    profile's recording_format.provenance: every NTSC/drop rate is 0x0013
    (19), every exact rate is 0x0010 (16)."""
    profile = CameraProfile.for_model("POCKET_6K_G2", "v8.6")

    assert set(profile.fps_modes) == {
        "23.98",
        "24",
        "25",
        "29.97",
        "30",
        "50",
        "59.94",
        "60",
    }

    exact_rates = ("24", "25", "30", "50", "60")
    ntsc_rates = ("23.98", "29.97", "59.94")
    for name in exact_rates:
        assert profile.require_fps_mode(name).m_rate == 0
        assert profile.require_fps_mode(name).frame_flags == 16
    for name in ntsc_rates:
        assert profile.require_fps_mode(name).m_rate == 1
        assert profile.require_fps_mode(name).frame_flags == 19

    # NTSC/drop rates report a rounded-up fps_int, not the fractional label.
    assert profile.require_fps_mode("23.98").fps_int == 24
    assert profile.require_fps_mode("29.97").fps_int == 30
    assert profile.require_fps_mode("59.94").fps_int == 60
    assert profile.require_fps_mode("60").fps_int == 60


def test_pocket_6k_g2_v86_profile_resolves_dimension_enums():
    """POCKET_6K_G2_v8.6.json's dimension_enums, from a full 0x00-0x1F active
    sweep via tools/control/sweep_dimension_enum.py (2026-07-30, docs/settings.md
    §18.9). All 8 confirmed enums independently match both POCKET_6K_G2_v7.9's
    and POCKET_6K_PRO_v8.6's numbers exactly — sniffed fresh on this firmware
    per design principle 6, not copied; the cross-profile agreement is the
    finding these assertions pin down, not the source of the values.

    HD's width/height came from this same sweep (its enum 0x03 report), not a
    separate passive re-capture — step 9's own HD window had caught the
    connect burst instead. commands.video_format is now VERIFIED: a
    480-combination CameraSession.set_camera_format() sweep (step 13,
    docs/settings.md §18.10) confirmed 432 combinations end to end, on top
    of two manual send_settings_command.py round trips."""
    profile = CameraProfile.for_model("POCKET_6K_G2", "v8.6")

    assert profile.require_command("video_format").provenance.status == "VERIFIED"

    hd = profile.require_resolution("HD")
    assert (hd.width, hd.height) == (1920, 1080)
    assert hd.dimension_enums == {"ProRes": 3}

    expected_enums = {
        "HD": {"ProRes": 3},
        "UHD": {"ProRes": 6},
        "4K DCI": {"BRAW": 8},  # ProRes enum still missing, same gap as v7.9/PRO
        "2.8K 17:9": {"BRAW": 13},
        "3.7K Anamorphic": {"BRAW": 15},
        "5.7K 17:9": {"BRAW": 18},
        "6K": {"BRAW": 19},
        "6K 2.4:1": {"BRAW": 20},
    }
    for name, enums in expected_enums.items():
        assert profile.require_resolution(name).dimension_enums == enums

    # 2.8K 17:9's width is settled for this firmware — sniffed, actively
    # probed, and confirmed on the camera's own on-screen display.
    assert profile.require_resolution("2.8K 17:9").width == 2880

    # 4K DCI/ProRes is known_unreachable here (2026-07-31, docs/settings.md
    # §18.12) — the enum-sweep gap alone wasn't sufficient on its own, but
    # v7.9's and the PRO's fuller write-value investigations have since been
    # repeated on this firmware and produced the same negative result.
    assert set(profile.require_resolution("4K DCI").known_unreachable) == {"ProRes"}

    # "30" was independently reconfirmed byte-identical across two separate
    # sniffer sessions (step 9's combined sweep and the dedicated fps sweep).
    assert profile.require_fps_mode("30").fps_int == 30


def test_pocket_6k_g2_v86_settings_families_promoted_to_verified():
    """Phase 3 steps 12/13 (2026-07-30, docs/settings.md §18.10): nine manual
    send_settings_command.py confirming writes plus a 480-combination
    sweep_camera_format.py sweep (432 confirmed) promoted all three settings
    families to VERIFIED — exceeding the single-round-trip bar
    (examples/change_codec.py) that promoted v7.9's equivalents.

    The sweep also surfaced two systematic gaps that mirror precedents already
    recorded on other profiles (POCKET_6K_PRO v8.6's ProRes/4K DCI
    known_unreachable and 6K max_fps_int=50, docs/settings.md §16/§17).
    resolutions.6K.max_fps_int was promoted the same day (2026-07-30): the
    operator confirmed on the camera's own UI that 6K doesn't offer
    59.94/60fps, meeting design principle 7's evidence bar. The ProRes/4K DCI
    known_unreachable entry was promoted 2026-07-31 (docs/settings.md §18.12):
    the operator ran the same three falsification hypotheses that closed the
    PRO's identical gap (data-type byte, Operation.OFFSET, exact fps/variant),
    three times each from three different starting states and Sensor Area
    settings — all 9 attempts stayed silent, meeting design principle 7's
    evidence bar the same way the PRO's entry did."""
    profile = CameraProfile.for_model("POCKET_6K_G2", "v8.6")

    for name in ("codec_quality", "video_format", "recording_format"):
        spec = profile.require_command(name)
        assert spec.provenance is not None
        assert spec.provenance.status == "VERIFIED"

    # ProRes/4K DCI: promoted 2026-07-31 after the operator exhausted the
    # same falsification hypotheses that closed the PRO's identical gap.
    assert set(profile.require_resolution("4K DCI").known_unreachable) == {"ProRes"}

    # BRAW@6K@59.94/60fps: promoted 2026-07-30 after an operator on-screen
    # confirmation met design principle 7's evidence bar for max_fps_int.
    assert profile.require_resolution("6K").max_fps_int == 50


def test_pocket_6k_g2_reserved_byte_differs_between_firmwares():
    """Design principle 6 in one assertion: the same command family on the same
    physical camera carries a different reserved byte across a firmware
    upgrade, so nothing may be inherited between profiles without re-sniffing."""
    v79 = CameraProfile.for_model("POCKET_6K_G2", "v7.9").require_command("recording")
    v86 = CameraProfile.for_model("POCKET_6K_G2", "v8.6").require_command("recording")

    assert (v79.category, v79.parameter) == (v86.category, v86.parameter)
    assert v79.values == v86.values
    assert v79.reserved == 1
    assert v86.reserved == 0


@pytest.mark.parametrize(("model_key", "firmware"), KNOWN_PROFILES)
def test_every_known_profile_resolves_write_margin_warning_storage_signal(model_key, firmware):
    """Every profile that has reached the sniffing phases carries an identical
    CANDIDATE write_margin_warning block — passive real-hardware evidence, not
    sent by this repo's code, see docs/recording.md's Camera-initiated stop
    detection section.

    A profile still at the Phase 1 scaffold stage (``_meta``/``ble`` only, no
    ``storage`` section at all) is skipped: it has nothing sniffed yet, and
    design principle 6 forbids copying another profile's values into it. Once
    the section exists, a missing or altered signal still fails here.
    """
    profile = CameraProfile.for_model(model_key, firmware)

    if not profile.storage:
        pytest.skip(f"{model_key}_{firmware} is a Phase 1 scaffold — nothing sniffed yet")

    spec = profile.require_storage_signal("write_margin_warning", ("nominal", "low_margin"))
    assert spec.category == 9
    assert spec.parameter == 1
    assert spec.byte_offset == 1
    assert spec.values == {"nominal": 1, "low_margin": -2}
    assert spec.provenance is not None
    assert spec.provenance.status == "CANDIDATE"
