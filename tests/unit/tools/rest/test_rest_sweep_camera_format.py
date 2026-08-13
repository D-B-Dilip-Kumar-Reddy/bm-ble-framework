"""Unit tests for tools/rest/sweep_camera_format.py's pure logic —
combination enumeration/filtering, live sensor-area expansion, per-item
outcome classification, and report saving.

No network, no input(), no hardware — matches tests/unit/'s "no hardware,
full mocking" rule. Real POCKET_6K_G2/POCKET_6K_PRO ble profiles are used
directly for enumerate_combinations (it only reads codecs/resolutions/
fps_modes, shared with BLE); expand_with_sensor_resolutions and run_combo
use a fake RestCameraSession-like object instead, since those need
supported_formats() and set_camera_format() rather than a real connection.

Loaded via importlib with an explicit module name rather than
tests/unit/tools/control/test_sweep_camera_format.py's plain
sys.path-insert-and-import pattern: tools/control/sweep_camera_format.py
and this file's target share the literal filename `sweep_camera_format.py`,
so a plain `import sweep_camera_format` collides in `sys.modules` across
the two test files when both run in the same pytest session — whichever
imports first silently wins for the rest of the run. Registering this
module under its own key before executing it avoids that collision (and,
as a side effect, avoids a `dataclass`/`from __future__ import annotations`
resolution error `module_from_spec` hits if the module is never registered
in `sys.modules` before `exec_module` runs).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[4] / "tools" / "rest" / "sweep_camera_format.py"
_spec = importlib.util.spec_from_file_location("rest_sweep_camera_format", _MODULE_PATH)
scf = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = scf
_spec.loader.exec_module(scf)

from bmd_camera.camera_profile import CameraProfile  # noqa: E402
from bmd_camera.exceptions import BMDUnsupportedError, BMDVerificationError  # noqa: E402


def _g2_profile() -> CameraProfile:
    return CameraProfile.for_model(model_key="POCKET_6K_G2", firmware="v8.6")


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

    def test_includes_known_unreachable_unlike_ble_tool(self):
        """Deliberately different from tools/control/sweep_camera_format.py:
        REST has no known_unreachable/max_fps_int filtering (design
        principle 7's REST sibling makes it unnecessary — see module
        docstring) — the PRO's real ProRes/4K DCI known_unreachable entry
        must still be enumerated here, unlike the BLE tool's default."""
        profile = _pro_profile()

        combos = scf.enumerate_combinations(profile, resolutions=["4K DCI"], codecs=["ProRes"])

        assert combos
        assert all(
            codec == "ProRes" and resolution == "4K DCI" for codec, _v, resolution, _f in combos
        )

    def test_includes_fps_above_max_fps_int_unlike_ble_tool(self):
        """PRO's real "6K" entry has max_fps_int=50 (docs/ble/settings.md) —
        this tool sweeps 59.94/60 there anyway, unlike the BLE tool's
        default exclusion, since REST validates live instead."""
        profile = _pro_profile()

        combos = scf.enumerate_combinations(profile, resolutions=["6K"])

        swept_fps = {fps for _c, _v, _r, fps in combos}
        assert "59.94" in swept_fps
        assert "60" in swept_fps

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

        assert scf.enumerate_combinations(profile) == scf.enumerate_combinations(profile)

    def test_full_sweep_matches_manually_computed_total(self):
        profile = _g2_profile()

        combos = scf.enumerate_combinations(profile)

        expected = sum(
            len(profile.require_codec(codec_name).variants) * len(profile.fps_modes)
            for resolution in profile.resolutions.values()
            for codec_name in resolution.codecs
        )
        assert len(combos) == expected


@dataclass(frozen=True)
class _FakeSupportedFormat:
    codecs: tuple[str, ...]
    frame_rates: tuple[str, ...]
    record_resolution: tuple[int, int]
    sensor_resolution: tuple[int, int]


def _format_profile() -> CameraProfile:
    """A minimal profile with the codecs/resolutions/fps_modes
    expand_with_sensor_resolutions and enumerate_combinations both need,
    and an empty (unconfirmed) format_names table so codec resolution
    exercises the derivation rule — mirrors
    tests/unit/rest/test_rest_session.py's make_format_profile."""
    ble_raw = {
        "_meta": {"model": "Pocket 6K G2", "ble_name": "A:TEST"},
        "codecs": {
            "BRAW": {"id": 3, "variants": {"5:1": 3}},
            "ProRes": {"id": 2, "variants": {"HQ": 0}},
        },
        "resolutions": {
            "4K DCI": {"width": 4096, "height": 2160, "codecs": ["BRAW", "ProRes"]},
            "HD": {"width": 1920, "height": 1080, "codecs": ["ProRes"]},
        },
        "fps_modes": {
            "23.98": {"fps_int": 24, "m_rate": 1, "frame_flags": 19},
        },
    }
    return CameraProfile._from_raw("POCKET_6K_G2", "v8.6", ble_raw)


class FakeSession:
    def __init__(self, formats, *, set_camera_format_side_effect=None):
        self._formats = formats
        self.set_camera_format_calls: list[tuple] = []
        self._side_effect = set_camera_format_side_effect

    async def supported_formats(self):
        return self._formats

    async def set_camera_format(self, codec, variant, resolution, fps, *, sensor_resolution=None):
        self.set_camera_format_calls.append((codec, variant, resolution, fps, sensor_resolution))
        if self._side_effect is not None:
            raise self._side_effect


class TestExpandWithSensorResolutions:
    @pytest.mark.asyncio
    async def test_single_match_expands_to_one_offered_item(self):
        profile = _format_profile()
        session = FakeSession(
            [
                _FakeSupportedFormat(
                    codecs=("BRaw:5_1",),
                    frame_rates=("23.98",),
                    record_resolution=(4096, 2160),
                    sensor_resolution=(4096, 2160),
                )
            ]
        )

        items = await scf.expand_with_sensor_resolutions(
            session, profile, [("BRAW", "5:1", "4K DCI", "23.98")]
        )

        assert items == [
            scf.SweepItem("BRAW", "5:1", "4K DCI", "23.98", (4096, 2160), offered=True)
        ]

    @pytest.mark.asyncio
    async def test_multiple_sensor_resolutions_expand_to_multiple_items(self):
        """Real case: ProRes at 1920x1080 pairs with three distinct
        sensorResolution values (docs/rest/transport.md) — every one must
        become its own sweep item, not just the first found."""
        profile = _format_profile()
        session = FakeSession(
            [
                _FakeSupportedFormat(
                    codecs=("ProRes:HQ",),
                    frame_rates=("23.98",),
                    record_resolution=(1920, 1080),
                    sensor_resolution=(2880, 1512),
                ),
                _FakeSupportedFormat(
                    codecs=("ProRes:HQ",),
                    frame_rates=("23.98",),
                    record_resolution=(1920, 1080),
                    sensor_resolution=(5376, 3024),
                ),
                _FakeSupportedFormat(
                    codecs=("ProRes:HQ",),
                    frame_rates=("23.98",),
                    record_resolution=(1920, 1080),
                    sensor_resolution=(6144, 3456),
                ),
            ]
        )

        items = await scf.expand_with_sensor_resolutions(
            session, profile, [("ProRes", "HQ", "HD", "23.98")]
        )

        assert len(items) == 3
        assert all(item.offered for item in items)
        assert {item.sensor_resolution for item in items} == {
            (2880, 1512),
            (5376, 3024),
            (6144, 3456),
        }
        # deterministic ordering — sorted by sensor_resolution
        assert [item.sensor_resolution for item in items] == [
            (2880, 1512),
            (5376, 3024),
            (6144, 3456),
        ]

    @pytest.mark.asyncio
    async def test_no_match_yields_single_unoffered_item(self):
        profile = _format_profile()
        session = FakeSession([])  # camera reports nothing supported

        items = await scf.expand_with_sensor_resolutions(
            session, profile, [("BRAW", "5:1", "4K DCI", "23.98")]
        )

        assert items == [scf.SweepItem("BRAW", "5:1", "4K DCI", "23.98", None, offered=False)]

    @pytest.mark.asyncio
    async def test_uses_confirmed_format_names_over_derivation(self):
        """format_names' confirmed mapping must be consulted, not just the
        derivation rule — mirrors the same real ProRes '422'->'Original'
        finding tests/unit/rest/test_rest_session.py pins."""
        ble_raw = {
            "_meta": {"model": "Pocket 6K G2", "ble_name": "A:TEST"},
            "codecs": {"ProRes": {"id": 2, "variants": {"422": 1}}},
            "resolutions": {
                "4K DCI": {"width": 4096, "height": 2160, "codecs": ["ProRes"]},
            },
            "fps_modes": {"23.98": {"fps_int": 24, "m_rate": 1, "frame_flags": 19}},
        }
        profile = CameraProfile._from_raw("POCKET_6K_G2", "v8.6", ble_raw)
        profile.rest = CameraProfile._rest_from_raw(
            {
                "_meta": {"model_key": "POCKET_6K_G2", "firmware": "v8.6", "status": "UNVERIFIED"},
                "endpoints": {},
                "format_names": {"ProRes": {"422": "ProRes:Original"}},
            }
        )
        session = FakeSession(
            [
                _FakeSupportedFormat(
                    codecs=("ProRes:Original",),
                    frame_rates=("23.98",),
                    record_resolution=(4096, 2160),
                    sensor_resolution=(5744, 3024),
                )
            ]
        )

        items = await scf.expand_with_sensor_resolutions(
            session, profile, [("ProRes", "422", "4K DCI", "23.98")]
        )

        assert items == [
            scf.SweepItem("ProRes", "422", "4K DCI", "23.98", (5744, 3024), offered=True)
        ]


class TestRunCombo:
    @pytest.mark.asyncio
    async def test_unoffered_item_skips_the_write_entirely(self):
        session = FakeSession([])
        item = scf.SweepItem("BRAW", "5:1", "4K DCI", "23.98", None, offered=False)

        result = await scf.run_combo(session, item)

        assert result.outcome == "unsupported"
        assert "no write attempted" in result.detail
        assert session.set_camera_format_calls == []

    @pytest.mark.asyncio
    async def test_confirmed_outcome_on_success(self):
        session = FakeSession([])
        item = scf.SweepItem("BRAW", "5:1", "4K DCI", "23.98", (4096, 2160), offered=True)

        result = await scf.run_combo(session, item)

        assert result.outcome == "confirmed"
        assert result.detail is None
        assert result.elapsed_s >= 0
        assert session.set_camera_format_calls == [("BRAW", "5:1", "4K DCI", "23.98", (4096, 2160))]

    @pytest.mark.asyncio
    async def test_unsupported_outcome_on_bmdunsupportederror(self):
        session = FakeSession([], set_camera_format_side_effect=BMDUnsupportedError("nope"))
        item = scf.SweepItem("ProRes", "HQ", "4K DCI", "23.98", (4096, 2160), offered=True)

        result = await scf.run_combo(session, item)

        assert result.outcome == "unsupported"
        assert result.detail == "nope"

    @pytest.mark.asyncio
    async def test_missing_data_outcome_on_valueerror(self):
        session = FakeSession([], set_camera_format_side_effect=ValueError("bad profile data"))
        item = scf.SweepItem("ProRes", "HQ", "4K DCI", "23.98", (4096, 2160), offered=True)

        result = await scf.run_combo(session, item)

        assert result.outcome == "missing_data"
        assert result.detail == "bad profile data"

    @pytest.mark.asyncio
    async def test_unconfirmed_outcome_on_bmdverificationerror(self):
        session = FakeSession([], set_camera_format_side_effect=BMDVerificationError("no event"))
        item = scf.SweepItem("ProRes", "HQ", "4K DCI", "23.98", (4096, 2160), offered=True)

        result = await scf.run_combo(session, item)

        assert result.outcome == "unconfirmed"
        assert result.detail == "no event"

    @pytest.mark.asyncio
    async def test_label_includes_sensor_resolution(self):
        session = FakeSession([])
        item = scf.SweepItem("BRAW", "5:1", "4K DCI", "23.98", (4096, 2160), offered=True)

        result = await scf.run_combo(session, item)

        assert result.label == "BRAW 5:1 4K DCI @ 23.98 [sensor 4096x2160]"


class TestSweepItemLabel:
    def test_unoffered_item_has_no_sensor_suffix(self):
        item = scf.SweepItem("BRAW", "5:1", "4K DCI", "23.98", None, offered=False)

        assert item.label == "BRAW 5:1 4K DCI @ 23.98"


class TestSaveReport:
    def test_writes_json_with_expected_shape(self, tmp_path):
        results = [
            scf.ComboResult(
                "BRAW", "5:1", "4K DCI", "23.98", (4096, 2160), "confirmed", None, 1.23
            ),
            scf.ComboResult(
                "ProRes", "HQ", "4K DCI", "23.98", None, "unsupported", "not offered", 0.0
            ),
        ]

        path = scf.save_report("POCKET_6K_G2", "v8.6", results, captures_dir=tmp_path)

        assert path.exists()
        assert path.parent == tmp_path / "rest" / "POCKET_6K_G2_v8.6"
        payload = json.loads(path.read_text())
        assert payload["model_key"] == "POCKET_6K_G2"
        assert payload["firmware"] == "v8.6"
        assert len(payload["results"]) == 2
        assert payload["results"][0]["outcome"] == "confirmed"
        assert payload["results"][0]["sensor_resolution"] == [4096, 2160]
