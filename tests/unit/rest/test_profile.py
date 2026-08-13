"""Unit tests for the REST half of :mod:`bmd_camera.camera_profile`.

Coverage:
  - validate_rest_profile schema rejections/acceptances (mirrors
    tests/unit/test_camera_profile.py's TestValidateProfile for the BLE
    schema)
  - CameraProfile._rest_from_raw parsing into RestEndpointSpec / RestProfile
  - rest_endpoint() / require_rest_endpoint() accessors
  - CameraProfile.for_model()'s REST loading: absent file -> empty
    RestProfile, present file -> parsed and _meta-cross-checked
  - Regression net: every real payloads/models/*/rest/*.json validates,
    has a matching ble/*.json for the same (model_key, firmware), and
    agrees with it on _meta.model_key / _meta.firmware
"""

from __future__ import annotations

import json

import pytest

from bmd_camera.camera_profile import (
    KNOWN_PROFILES,
    MODELS_DIR,
    CameraProfile,
    RestEndpointSpec,
    validate_rest_profile,
)


def make_valid_rest_raw(**overrides) -> dict:
    """A minimal schema-valid REST profile dict."""
    raw = {
        "_meta": {
            "model_key": "POCKET_6K_PRO",
            "firmware": "v8.6",
            "status": "UNVERIFIED",
        },
        "transport": "usb",
        "endpoints": {
            "/system/format": {
                "status": 200,
                "supported": True,
                "put_status": 204,
                "put_supported": True,
            }
        },
        "websocket_properties": ["/system/format"],
        "format_names": {},
        "provenance": {"status": "CANDIDATE"},
    }
    raw.update(overrides)
    return raw


class TestValidateRestProfile:
    def test_accepts_valid_profile(self):
        validate_rest_profile(make_valid_rest_raw(), source="test_rest.json")

    def test_rejects_missing_meta_field(self):
        raw = make_valid_rest_raw()
        del raw["_meta"]["status"]
        with pytest.raises(ValueError, match="status"):
            validate_rest_profile(raw, source="test_rest.json")

    def test_rejects_bad_status_enum(self):
        raw = make_valid_rest_raw()
        raw["_meta"]["status"] = "MOSTLY_VERIFIED"
        with pytest.raises(ValueError, match="status"):
            validate_rest_profile(raw, source="test_rest.json")

    def test_rejects_unknown_key_in_endpoint_entry(self):
        raw = make_valid_rest_raw()
        raw["endpoints"]["/system/format"]["statuss"] = 200
        with pytest.raises(ValueError, match="statuss"):
            validate_rest_profile(raw, source="test_rest.json")

    def test_rejects_bad_transport_enum(self):
        raw = make_valid_rest_raw()
        raw["transport"] = "wifi"
        with pytest.raises(ValueError, match="transport"):
            validate_rest_profile(raw, source="test_rest.json")

    def test_rejects_provenance_without_status(self):
        raw = make_valid_rest_raw()
        raw["provenance"] = {"method": "manual"}
        with pytest.raises(ValueError, match="status"):
            validate_rest_profile(raw, source="test_rest.json")

    def test_accepts_null_status_for_unreachable_endpoint(self):
        raw = make_valid_rest_raw()
        raw["endpoints"]["/audio/channel/2/input"] = {
            "status": None,
            "supported": False,
            "notes": "Connection refused",
        }
        validate_rest_profile(raw, source="test_rest.json")

    def test_allows_comment_keys_anywhere(self):
        raw = make_valid_rest_raw()
        raw["_comment"] = "top-level note"
        raw["endpoints"]["_comment"] = "endpoints note"
        validate_rest_profile(raw, source="test_rest.json")

    def test_error_message_names_source_file(self):
        raw = make_valid_rest_raw()
        raw["_meta"]["status"] = "BOGUS"
        with pytest.raises(ValueError, match="test_rest.json"):
            validate_rest_profile(raw, source="test_rest.json")


class TestRestFromRaw:
    def test_parses_endpoints(self):
        rest = CameraProfile._rest_from_raw(make_valid_rest_raw())

        spec = rest.endpoints["/system/format"]
        assert isinstance(spec, RestEndpointSpec)
        assert spec.status == 200
        assert spec.supported is True
        assert spec.put_status == 204
        assert spec.put_supported is True

    def test_parses_meta_status_and_transport(self):
        rest = CameraProfile._rest_from_raw(make_valid_rest_raw())

        assert rest.status == "UNVERIFIED"
        assert rest.transport == "usb"

    def test_parses_websocket_properties(self):
        rest = CameraProfile._rest_from_raw(make_valid_rest_raw())

        assert rest.websocket_properties == ("/system/format",)

    def test_parses_provenance(self):
        rest = CameraProfile._rest_from_raw(make_valid_rest_raw())

        assert rest.provenance is not None
        assert rest.provenance.status == "CANDIDATE"

    def test_null_status_endpoint_parses_to_none(self):
        raw = make_valid_rest_raw()
        raw["endpoints"]["/audio/channel/2/input"] = {
            "status": None,
            "supported": False,
            "notes": "Connection refused",
        }
        rest = CameraProfile._rest_from_raw(raw)

        spec = rest.endpoints["/audio/channel/2/input"]
        assert spec.status is None
        assert spec.notes == "Connection refused"

    def test_comment_keys_are_skipped(self):
        raw = make_valid_rest_raw()
        raw["endpoints"]["_comment"] = "note"
        rest = CameraProfile._rest_from_raw(raw)

        assert set(rest.endpoints) == {"/system/format"}

    def test_format_names_parsed_and_comment_keys_skipped(self):
        raw = make_valid_rest_raw(
            format_names={
                "BRAW": {"5:1": "BRaw:5_1", "_comment": "skip me"},
                "_comment": "skip me too",
            }
        )
        rest = CameraProfile._rest_from_raw(raw)

        assert rest.format_names == {"BRAW": {"5:1": "BRaw:5_1"}}


class TestRestEndpointAccess:
    def test_rest_endpoint_returns_none_when_absent(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", {})

        assert profile.rest_endpoint("/system/format") is None

    def test_require_rest_endpoint_raises_naming_missing_path(self):
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v7.9", {})

        with pytest.raises(ValueError, match="no REST endpoint '/system/format'"):
            profile.require_rest_endpoint("/system/format")

    def test_require_rest_endpoint_returns_spec_when_present(self):
        profile = CameraProfile._from_raw("POCKET_6K_PRO", "v8.6", {})
        profile.rest = CameraProfile._rest_from_raw(make_valid_rest_raw())

        spec = profile.require_rest_endpoint("/system/format")

        assert spec.put_supported is True


class TestForModelRestLoading:
    def test_profile_without_rest_file_has_empty_rest_profile(self):
        """POCKET_6K_G2 v7.9 has no rest/ file yet — for_model must still
        succeed, with an all-defaults RestProfile (mirrors a Phase 1 BLE
        scaffold's commands == {})."""
        profile = CameraProfile.for_model("POCKET_6K_G2", "v7.9")

        assert profile.rest.status == "UNKNOWN"
        assert profile.rest.endpoints == {}

    def test_profile_with_rest_file_loads_real_endpoints(self):
        profile = CameraProfile.for_model("POCKET_6K_PRO", "v8.6")

        assert profile.rest.transport == "usb"
        assert profile.rest.endpoints  # non-empty
        fmt = profile.require_rest_endpoint("/system/format")
        assert fmt.put_status == 204

    def test_meta_identity_mismatch_raises(self, tmp_path, monkeypatch):
        models_dir = tmp_path / "models"
        rest_dir = models_dir / "POCKET_6K_PRO" / "rest"
        rest_dir.mkdir(parents=True)
        raw = make_valid_rest_raw()
        raw["_meta"]["firmware"] = "v7.9"  # disagrees with the requested v8.6
        (rest_dir / "v8.6.json").write_text(json.dumps(raw), encoding="utf-8")
        monkeypatch.setattr("bmd_camera.camera_profile.MODELS_DIR", models_dir)

        with pytest.raises(ValueError, match="_meta.firmware"):
            CameraProfile._load_rest_profile("POCKET_6K_PRO", "v8.6")

    def test_missing_rest_file_returns_empty_profile_without_error(self, tmp_path, monkeypatch):
        models_dir = tmp_path / "models"
        models_dir.mkdir(parents=True)
        monkeypatch.setattr("bmd_camera.camera_profile.MODELS_DIR", models_dir)

        rest = CameraProfile._load_rest_profile("POCKET_6K_G2", "v8.6")

        assert rest.endpoints == {}
        assert rest.status == "UNKNOWN"


def _discover_rest_profiles() -> list[tuple[str, str]]:
    """Every (model_key, firmware) pair with a real rest/<fw>.json on disk."""
    pairs = []
    for model_dir in sorted(MODELS_DIR.glob("*")):
        rest_dir = model_dir / "rest"
        if not rest_dir.is_dir():
            continue
        for rest_file in sorted(rest_dir.glob("*.json")):
            pairs.append((model_dir.name, rest_file.stem))
    return pairs


REST_PROFILES = _discover_rest_profiles()


class TestRealRestProfiles:
    def test_at_least_one_real_rest_profile_exists(self):
        """Regression net for this test module itself: if the discovery glob
        ever silently finds nothing, the parametrized test below would pass
        vacuously and hide a real regression."""
        assert REST_PROFILES

    @pytest.mark.parametrize(("model_key", "firmware"), REST_PROFILES)
    def test_real_rest_profile_validates_and_loads(self, model_key, firmware):
        profile = CameraProfile.for_model(model_key, firmware)

        assert profile.rest.endpoints
        assert profile.rest.transport in ("usb", "lan")

    @pytest.mark.parametrize(("model_key", "firmware"), REST_PROFILES)
    def test_real_rest_profile_has_a_matching_ble_profile(self, model_key, firmware):
        """Every rest/<fw>.json profile's (model_key, firmware) must also
        have a ble/<fw>.json sibling — REST is additive to a camera already
        brought up over BLE, not a replacement bring-up path."""
        assert (model_key, firmware) in KNOWN_PROFILES

    @pytest.mark.parametrize(("model_key", "firmware"), REST_PROFILES)
    def test_real_rest_profile_meta_agrees_with_ble_profile(self, model_key, firmware):
        rest_path = MODELS_DIR / model_key / "rest" / f"{firmware}.json"
        ble_path = MODELS_DIR / model_key / "ble" / f"{firmware}.json"
        rest_raw = json.loads(rest_path.read_text(encoding="utf-8"))
        ble_raw = json.loads(ble_path.read_text(encoding="utf-8"))

        assert rest_raw["_meta"]["model_key"] == ble_raw["_meta"]["model_key"]
        assert rest_raw["_meta"]["firmware"] == ble_raw["_meta"]["firmware"]
