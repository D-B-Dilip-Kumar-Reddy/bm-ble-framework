"""Unit tests for tools/control/sweep_camera_format.py's pure logic —
combination enumeration/filtering, per-combo outcome classification, and
report saving.

No BLE, no input(), no hardware — matches tests/unit/'s "no hardware, full
mocking" rule and tests/unit/tools/control/test_sweep_dimension_enum.py's
sys.path pattern for importing a standalone (non-package) tools/ script.
CameraProfile.for_model() reads local profile JSON only — no network/BLE —
so real G2/PRO profiles are used directly, the same way
test_sweep_dimension_enum.py exercises them. The PRO profile in particular
already has a real known_unreachable entry (ProRes/4K DCI, docs/ble/settings.md
§16), which is exactly what the exclusion tests need — no fixture profile
has to fake one up.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "common"))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "control"))

import sweep_camera_format as scf  # noqa: E402

from bmd_camera.camera_profile import CameraProfile  # noqa: E402
from bmd_camera.exceptions import BMDUnsupportedError, BMDVerificationError  # noqa: E402


def _g2_profile() -> CameraProfile:
    return CameraProfile.for_model(model_key="POCKET_6K_G2", firmware="v7.9")


def _pro_profile() -> CameraProfile:
    return CameraProfile.for_model(model_key="POCKET_6K_PRO", firmware="v8.6")


class TestEnumerateCombinations:
    def test_filters_by_resolution(self):
        profile = _g2_profile()

        combos = scf.enumerate_combinations(profile, resolutions=["4K DCI"])

        assert combos
        assert all(resolution == "4K DCI" for _c, _v, resolution, _f in combos)

    def test_filters_by_codec(self):
        profile = _g2_profile()

        combos = scf.enumerate_combinations(profile, codecs=["ProRes"])

        assert combos
        assert all(codec == "ProRes" for codec, _v, _r, _f in combos)

    def test_filters_by_fps(self):
        profile = _g2_profile()

        combos = scf.enumerate_combinations(profile, fps_modes=["25"])

        assert combos
        assert all(fps == "25" for _c, _v, _r, fps in combos)

    def test_variant_filter_excludes_codecs_with_no_matching_variant(self):
        profile = _g2_profile()

        # "HQ" is a ProRes variant name, never a BRAW one — BRAW should
        # contribute zero combinations, not raise, when filtered to it.
        combos = scf.enumerate_combinations(profile, variants=["HQ"])

        assert combos
        assert all(codec == "ProRes" for codec, _v, _r, _f in combos)
        assert all(variant == "HQ" for _c, variant, _r, _f in combos)

    def test_narrowed_combination_is_exact(self):
        profile = _g2_profile()

        combos = scf.enumerate_combinations(
            profile,
            resolutions=["4K DCI"],
            codecs=["BRAW"],
            variants=["5:1"],
            fps_modes=["25"],
        )

        assert combos == [("BRAW", "5:1", "4K DCI", "25")]

    def test_excludes_known_unreachable_by_default(self):
        profile = _pro_profile()

        combos = scf.enumerate_combinations(profile, resolutions=["4K DCI"], codecs=["ProRes"])

        assert combos == []

    def test_include_known_unreachable_includes_it(self):
        profile = _pro_profile()

        combos = scf.enumerate_combinations(
            profile, resolutions=["4K DCI"], codecs=["ProRes"], include_known_unreachable=True
        )

        assert combos
        assert all(
            codec == "ProRes" and resolution == "4K DCI" for codec, _v, resolution, _f in combos
        )

    def test_known_unreachable_does_not_affect_other_codecs_at_same_resolution(self):
        profile = _pro_profile()

        combos = scf.enumerate_combinations(profile, resolutions=["4K DCI"], codecs=["BRAW"])

        assert combos
        assert all(codec == "BRAW" for codec, _v, _r, _f in combos)

    def test_excludes_fps_above_max_fps_int_by_default(self):
        # PRO's real "6K" entry has max_fps_int=50 (docs/ble/settings.md) —
        # 59.94/60 (fps_int=60) must not appear by default.
        profile = _pro_profile()

        combos = scf.enumerate_combinations(profile, resolutions=["6K"])

        swept_fps = {fps for _c, _v, _r, fps in combos}
        assert "59.94" not in swept_fps
        assert "60" not in swept_fps
        assert "50" in swept_fps

    def test_include_unsupported_fps_includes_it(self):
        profile = _pro_profile()

        combos = scf.enumerate_combinations(
            profile, resolutions=["6K"], include_unsupported_fps=True
        )

        swept_fps = {fps for _c, _v, _r, fps in combos}
        assert "59.94" in swept_fps
        assert "60" in swept_fps

    def test_max_fps_int_does_not_affect_resolutions_without_it(self):
        # "4K DCI" has no max_fps_int in the real PRO profile — every fps
        # (including 59.94/60) should still be swept there for BRAW.
        profile = _pro_profile()

        combos = scf.enumerate_combinations(profile, resolutions=["4K DCI"], codecs=["BRAW"])

        swept_fps = {fps for _c, _v, _r, fps in combos}
        assert "59.94" in swept_fps
        assert "60" in swept_fps

    def test_explicit_fps_filter_above_ceiling_yields_no_combos(self):
        profile = _pro_profile()

        combos = scf.enumerate_combinations(profile, resolutions=["6K"], fps_modes=["60"])

        assert combos == []

    def test_unknown_resolution_filter_raises(self):
        profile = _g2_profile()

        with pytest.raises(ValueError, match="no resolution 'Nonexistent'"):
            scf.enumerate_combinations(profile, resolutions=["Nonexistent"])

    def test_unknown_codec_filter_raises(self):
        profile = _g2_profile()

        with pytest.raises(ValueError, match="no codec 'H265'"):
            scf.enumerate_combinations(profile, codecs=["H265"])

    def test_unknown_fps_filter_raises(self):
        profile = _g2_profile()

        with pytest.raises(ValueError, match="no fps mode '999'"):
            scf.enumerate_combinations(profile, fps_modes=["999"])

    def test_deterministic_ordering_across_calls(self):
        profile = _g2_profile()

        first = scf.enumerate_combinations(profile)
        second = scf.enumerate_combinations(profile)

        assert first == second

    def test_full_sweep_matches_manually_computed_total(self):
        profile = _g2_profile()

        combos = scf.enumerate_combinations(profile)

        expected = sum(
            len(profile.require_codec(codec_name).variants) * len(profile.fps_modes)
            for resolution in profile.resolutions.values()
            for codec_name in resolution.codecs
        )
        assert len(combos) == expected


class TestRunCombo:
    @pytest.mark.asyncio
    async def test_confirmed_outcome_on_success(self):
        session = AsyncMock()
        session.set_camera_format = AsyncMock(return_value=None)

        result = await scf.run_combo(session, "BRAW", "5:1", "4K DCI", "25")

        assert result.outcome == "confirmed"
        assert result.detail is None
        assert result.elapsed_s >= 0
        session.set_camera_format.assert_awaited_once_with("BRAW", "5:1", "4K DCI", "25")

    @pytest.mark.asyncio
    async def test_unsupported_outcome_on_bmdunsupportederror(self):
        session = AsyncMock()
        session.set_camera_format = AsyncMock(side_effect=BMDUnsupportedError("nope"))

        result = await scf.run_combo(session, "ProRes", "HQ", "4K DCI", "25")

        assert result.outcome == "unsupported"
        assert result.detail == "nope"

    @pytest.mark.asyncio
    async def test_missing_data_outcome_on_valueerror(self):
        session = AsyncMock()
        session.set_camera_format = AsyncMock(side_effect=ValueError("no dimension_enum"))

        result = await scf.run_combo(session, "ProRes", "HQ", "4K DCI", "25")

        assert result.outcome == "missing_data"
        assert result.detail == "no dimension_enum"

    @pytest.mark.asyncio
    async def test_unconfirmed_outcome_on_bmdverificationerror(self):
        session = AsyncMock()
        session.set_camera_format = AsyncMock(side_effect=BMDVerificationError("no echo"))

        result = await scf.run_combo(session, "ProRes", "HQ", "4K DCI", "25")

        assert result.outcome == "unconfirmed"
        assert result.detail == "no echo"

    @pytest.mark.asyncio
    async def test_label_property_formats_the_combination(self):
        session = AsyncMock()
        session.set_camera_format = AsyncMock(return_value=None)

        result = await scf.run_combo(session, "BRAW", "5:1", "4K DCI", "25")

        assert result.label == "BRAW 5:1 4K DCI @ 25"


class TestSaveReport:
    def test_writes_json_with_expected_shape(self, tmp_path):
        results = [
            scf.ComboResult("BRAW", "5:1", "4K DCI", "25", "confirmed", None, 1.23),
            scf.ComboResult("ProRes", "HQ", "4K DCI", "25", "unconfirmed", "no echo", 3.21),
        ]

        path = scf.save_report("POCKET_6K_PRO", "v8.6", results, captures_dir=tmp_path)

        assert path.exists()
        payload = json.loads(path.read_text())
        assert payload["model_key"] == "POCKET_6K_PRO"
        assert payload["firmware"] == "v8.6"
        assert len(payload["results"]) == 2
        assert payload["results"][1]["outcome"] == "unconfirmed"
        assert payload["results"][1]["detail"] == "no echo"

    def test_creates_the_model_firmware_subdirectory(self, tmp_path):
        path = scf.save_report("POCKET_6K_PRO", "v8.6", [], captures_dir=tmp_path)

        assert path.parent == tmp_path / "POCKET_6K_PRO_v8.6"
