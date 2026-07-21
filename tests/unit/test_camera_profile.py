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


def test_pocket_6k_pro_profile_has_no_transcribed_settings_values_yet():
    """Settings values must never be copied across models without sniffing
    that camera (CLAUDE.md design principle 6) — codec_quality/
    recording_format and the codecs/resolutions lookup tables stay
    untranscribed until captured on the PRO itself. video_format's
    category/parameter/data_type/reserved exist as an explicit CANDIDATE
    hypothesis (mirroring the G2's coordinates, since video_format never
    reports passively on either camera — see docs/settings.md §14/PRO
    section) and fps_modes has one wire-observed entry ("50") — neither
    is a copied *value*, both are pending confirmation via active send."""
    profile = CameraProfile.for_model("POCKET_6K_PRO", "v8.6")

    assert profile.command("codec_quality") is None
    assert profile.command("recording_format") is None
    video_format = profile.command("video_format")
    assert video_format is not None
    assert video_format.provenance.status == "CANDIDATE"
    assert profile.codecs == {}
    assert profile.resolutions == {}
    assert set(profile.fps_modes) == {"50"}


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


@pytest.mark.parametrize(("model_key", "firmware"), KNOWN_PROFILES)
def test_every_known_profile_resolves_write_margin_warning_storage_signal(model_key, firmware):
    """Both real profiles carry an identical CANDIDATE write_margin_warning
    block — passive real-hardware evidence, not sent by this repo's code,
    see docs/recording.md's Camera-initiated stop detection section."""
    profile = CameraProfile.for_model(model_key, firmware)

    spec = profile.require_storage_signal("write_margin_warning", ("nominal", "low_margin"))
    assert spec.category == 9
    assert spec.parameter == 1
    assert spec.byte_offset == 1
    assert spec.values == {"nominal": 1, "low_margin": -2}
    assert spec.provenance is not None
    assert spec.provenance.status == "CANDIDATE"
