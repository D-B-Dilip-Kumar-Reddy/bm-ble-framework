"""Unit tests for :mod:`bmd_camera.rest.mapping`."""

from __future__ import annotations

from bmd_camera.rest.mapping import derive_rest_codec_name, resolve_rest_codec_name


class TestDeriveRestCodecName:
    def test_braw_colon_to_underscore_is_a_confirmed_rule(self):
        """docs/rest/transport.md's "Codec naming" table, POCKET_6K_G2 v8.6."""
        assert derive_rest_codec_name("BRAW", "5:1") == "BRaw:5_1"
        assert derive_rest_codec_name("BRAW", "12:1") == "BRaw:12_1"

    def test_braw_family_name_gets_rest_casing(self):
        assert derive_rest_codec_name("BRAW", "Q0") == "BRaw:Q0"

    def test_prores_family_name_unchanged(self):
        assert derive_rest_codec_name("ProRes", "HQ") == "ProRes:HQ"

    def test_prores_422_derivation_is_known_wrong(self):
        """Real evidence: REST spells this variant "Original", not "422" —
        this derivation cannot know that; format_names must carry the real
        string instead (see resolve_rest_codec_name)."""
        assert derive_rest_codec_name("ProRes", "422") == "ProRes:422"

    def test_unknown_family_passes_through_unchanged(self):
        assert derive_rest_codec_name("H265", "Standard") == "H265:Standard"


class TestResolveRestCodecName:
    def test_prefers_confirmed_format_names_entry(self):
        format_names = {"ProRes": {"422": "Original"}}
        assert resolve_rest_codec_name(format_names, "ProRes", "422") == "Original"

    def test_falls_back_to_derivation_when_unconfirmed(self):
        assert resolve_rest_codec_name({}, "BRAW", "5:1") == "BRaw:5_1"

    def test_falls_back_when_family_present_but_variant_missing(self):
        format_names = {"BRAW": {"Q0": "BRaw:Q0"}}
        assert resolve_rest_codec_name(format_names, "BRAW", "5:1") == "BRaw:5_1"
