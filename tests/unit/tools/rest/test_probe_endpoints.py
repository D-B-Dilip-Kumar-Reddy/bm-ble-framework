"""Unit tests for tools/rest/probe_endpoints.py — pure sweep logic only.

No HTTP, no camera, no aiohttp required: every function exercised here is
side-effect-free. tools/ is not a package, so tools/rest is added to
sys.path directly (same pattern as tests/unit/tools/common/test_discovery.py).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "rest"))

from probe_endpoints import (  # noqa: E402
    NEVER_WRITE,
    SPEC_PROPERTIES,
    Endpoint,
    ProbeResult,
    Report,
    build_catalog,
    build_rest_profile_block,
    classify,
    device_names_from_workingset,
    echo_body,
    expand_catalog,
    extract_links,
    is_supported,
    parse_windows_gateways,
    pick_first,
    render_summary,
    subscribable_properties,
    transport_mode_body,
    ws_response_ok,
)


def make_report(**overrides) -> Report:
    defaults = {
        "model_key": "POCKET_6K_G2",
        "firmware": "v8.6",
        "transport": "usb",
        "host": "192.168.1.1",
        "probed_at": "2026-07-31T12-00-00Z",
        "probe_writes": False,
    }
    defaults.update(overrides)
    return Report(**defaults)


class TestClassify:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (200, "ok"),
            (204, "ok"),
            (299, "ok"),
            (404, "not_found"),
            (500, "server_error"),
            (503, "server_error"),
            (501, "not_implemented"),
            (400, "other"),
            (301, "other"),
        ],
    )
    def test_status_buckets(self, status, expected):
        assert classify(status) == expected

    def test_501_is_not_lumped_in_with_other_5xx(self):
        """The whole point of the sweep: "this device doesn't do that" is a
        different fact from "this firmware is broken here"."""
        assert classify(501) != classify(500)

    def test_no_reply_is_unreachable(self):
        assert classify(None) == "unreachable"

    def test_error_wins_over_status(self):
        assert classify(200, "ClientError: boom") == "unreachable"

    @pytest.mark.parametrize(
        ("classification", "expected"),
        [
            ("ok", True),
            ("server_error", False),
            ("not_implemented", False),
            ("not_found", False),
            ("unreachable", False),
            ("other", False),
        ],
    )
    def test_only_ok_is_supported(self, classification, expected):
        assert is_supported(classification) is expected

    def test_probe_result_exposes_classification(self):
        assert ProbeResult("GET", "/system", 501).classification == "not_implemented"


class TestCatalog:
    def test_covers_every_operator_verified_endpoint(self):
        paths = {endpoint.path for endpoint in build_catalog()}
        for path in (
            "/system/format",
            "/video/iso",
            "/video/whiteBalance",
            "/video/shutter",
            "/media/workingset",
            "/clips/list",
        ):
            assert path in paths

    def test_covers_the_known_broken_endpoints(self):
        paths = {endpoint.path for endpoint in build_catalog()}
        for path in ("/system", "/system/videoFormat", "/system/codecFormat"):
            assert path in paths

    def test_destructive_paths_have_no_write_builder(self):
        """NEVER_WRITE is documentation; this asserts the catalog agrees with
        it, so adding a builder to a destructive endpoint fails here rather
        than on real hardware."""
        for endpoint in build_catalog():
            if endpoint.path in NEVER_WRITE:
                assert endpoint.write_body is None, endpoint.path

    def test_record_and_playback_transport_paths_are_never_writable(self):
        by_path = {endpoint.path: endpoint for endpoint in build_catalog()}
        for path in ("/transports/0/record", "/transports/0/play", "/transports/0/stop"):
            assert by_path[path].write_body is None

    def test_doformat_is_never_writable(self):
        by_path = {endpoint.path: endpoint for endpoint in build_catalog()}
        assert by_path["/media/devices/{deviceName}/doformat"].write_body is None

    def test_audio_channels_are_expanded_per_index(self):
        paths = {endpoint.path for endpoint in build_catalog()}
        assert "/audio/channel/0/level" in paths
        assert "/audio/channel/1/level" in paths

    def test_no_duplicate_paths(self):
        paths = [endpoint.path for endpoint in build_catalog()]
        assert len(paths) == len(set(paths))


class TestExpandCatalog:
    def test_fills_device_name_template_once_per_device(self):
        catalog = [Endpoint("/media/devices/{deviceName}"), Endpoint("/system/format")]
        expanded = expand_catalog(catalog, ["sd1", "sd2"])
        paths = [endpoint.path for endpoint in expanded]
        assert paths == ["/media/devices/sd1", "/media/devices/sd2", "/system/format"]

    def test_drops_templates_when_no_devices_are_known(self):
        """Requesting the literal path `{deviceName}` would record a 404 that
        says nothing about the camera."""
        expanded = expand_catalog([Endpoint("/media/devices/{deviceName}")], [])
        assert expanded == []

    def test_preserves_write_builder_and_note(self):
        catalog = [Endpoint("/media/devices/{deviceName}", echo_body, note="hi")]
        expanded = expand_catalog(catalog, ["sd1"])
        assert expanded[0].write_body is echo_body
        assert expanded[0].note == "hi"


class TestDeviceNames:
    def test_reads_names_in_workingset_order(self):
        payload = {
            "size": 2,
            "workingset": [{"index": 0, "deviceName": "sd1"}, {"index": 1, "deviceName": "sd2"}],
        }
        assert device_names_from_workingset(payload) == ["sd1", "sd2"]

    def test_deduplicates(self):
        payload = {"workingset": [{"deviceName": "sd1"}, {"deviceName": "sd1"}]}
        assert device_names_from_workingset(payload) == ["sd1"]

    @pytest.mark.parametrize(
        "payload", [None, {}, "text", {"workingset": None}, {"workingset": []}]
    )
    def test_tolerates_missing_or_malformed_bodies(self, payload):
        assert device_names_from_workingset(payload) == []

    def test_skips_entries_without_a_string_name(self):
        payload = {"workingset": [{"index": 0}, {"deviceName": 7}, {"deviceName": "sd1"}]}
        assert device_names_from_workingset(payload) == ["sd1"]


class TestWriteBodyBuilders:
    def test_echo_returns_a_copy(self):
        payload = {"iso": 400}
        built = echo_body(payload)
        assert built == {"iso": 400}
        assert built is not payload

    @pytest.mark.parametrize("payload", [None, {}, [], "text"])
    def test_echo_declines_empty_or_non_dict(self, payload):
        assert echo_body(payload) is None

    def test_pick_first_honours_the_documented_priority_order(self):
        """`/lens/iris` reports three fields but its PUT documents
        apertureStop > normalised > apertureNumber."""
        build = pick_first("apertureStop", "normalised", "apertureNumber")
        payload = {"apertureStop": 4.0, "normalised": 0.5, "apertureNumber": 2.8}
        assert build(payload) == {"apertureStop": 4.0}

    def test_pick_first_falls_through_to_the_next_present_key(self):
        build = pick_first("shutterSpeed", "shutterAngle")
        assert build({"shutterAngle": 18000}) == {"shutterAngle": 18000}

    def test_pick_first_ignores_null_values(self):
        build = pick_first("shutterSpeed", "shutterAngle")
        assert build({"shutterSpeed": None, "shutterAngle": 18000}) == {"shutterAngle": 18000}

    def test_pick_first_declines_when_no_key_is_present(self):
        build = pick_first("gain", "normalised")
        assert build({"other": 1}) is None

    def test_shutter_builder_drops_the_read_only_field(self):
        """GET reports continuousShutterAutoExposure; the PUT schema has no
        such field."""
        build = pick_first("shutterSpeed", "shutterAngle")
        payload = {"continuousShutterAutoExposure": False, "shutterAngle": 18000}
        assert build(payload) == {"shutterAngle": 18000}

    @pytest.mark.parametrize("mode", ["InputPreview", "Output"])
    def test_transport_mode_echoes_settable_modes(self, mode):
        assert transport_mode_body({"mode": mode}) == {"mode": mode}

    def test_transport_mode_declines_input_record(self):
        """PUT /transports/0 accepts only InputPreview and Output — and a
        camera reporting InputRecord is mid-take."""
        assert transport_mode_body({"mode": "InputRecord"}) is None

    @pytest.mark.parametrize("payload", [None, {}, {"mode": "Nonsense"}, "text"])
    def test_transport_mode_declines_anything_else(self, payload):
        assert transport_mode_body(payload) is None


class TestSubscribableProperties:
    def test_reads_the_events_array(self):
        payload = {"events": ["/media/active", "/system/format"]}
        assert subscribable_properties(payload) == ["/media/active", "/system/format"]

    def test_accepts_a_bare_list(self):
        assert subscribable_properties(["/transports/0/record"]) == ["/transports/0/record"]

    @pytest.mark.parametrize("payload", [None, {}, "text", {"events": None}])
    def test_tolerates_missing_or_malformed_bodies(self, payload):
        assert subscribable_properties(payload) == []

    def test_spec_properties_include_the_operator_confirmed_three(self):
        for prop in ("/media/active", "/system/format", "/transports/0/record"):
            assert prop in SPEC_PROPERTIES


class TestWsResponseOk:
    def test_accepts_success_at_the_top_level(self):
        """The shape the camera was actually observed returning."""
        assert ws_response_ok({"type": "response", "success": True}) is True

    def test_accepts_success_nested_under_data(self):
        """The shape Notification.yaml documents."""
        assert ws_response_ok({"type": "response", "data": {"success": True}}) is True

    def test_reports_failure(self):
        assert ws_response_ok({"type": "response", "success": False}) is False
        assert ws_response_ok({"type": "response", "data": {"success": False}}) is False

    @pytest.mark.parametrize("message", [None, {}, "text", {"type": "response"}])
    def test_missing_success_is_not_success(self, message):
        assert ws_response_ok(message) is False


class TestExtractLinks:
    def test_pulls_directory_entries(self):
        body = '<a href="A001-sd1/">A001-sd1</a><a href="A002-sd2/">A002-sd2</a>'
        assert extract_links(body) == ["A001-sd1/", "A002-sd2/"]

    def test_drops_parent_and_absolute_links(self):
        body = (
            '<a href="..">up</a><a href="/">root</a><a href="http://x/y">ext</a><a href="Z/">Z</a>'
        )
        assert extract_links(body) == ["Z/"]

    def test_deduplicates(self):
        assert extract_links('<a href="A/">A</a><a href="A/">A</a>') == ["A/"]

    @pytest.mark.parametrize("body", [None, {}, 7])
    def test_non_text_bodies_yield_nothing(self, body):
        assert extract_links(body) == []


class TestParseWindowsGateways:
    def test_extracts_ipv4_gateways(self):
        output = "\n".join(
            [
                "Ethernet adapter Ethernet 2:",
                "   Default Gateway . . . . . . . . . : 192.168.1.1",
                "Wireless LAN adapter Wi-Fi:",
                "   Default Gateway . . . . . . . . . : 10.0.0.1",
            ]
        )
        assert parse_windows_gateways(output) == ["192.168.1.1", "10.0.0.1"]

    def test_skips_blank_and_ipv6_gateways(self):
        output = "\n".join(
            [
                "   Default Gateway . . . . . . . . . :",
                "   Default Gateway . . . . . . . . . : fe80::1%12",
                "   Default Gateway . . . . . . . . . : 192.168.1.1",
            ]
        )
        assert parse_windows_gateways(output) == ["192.168.1.1"]

    def test_deduplicates(self):
        output = "   Default Gateway : 192.168.1.1\n   Default Gateway : 192.168.1.1"
        assert parse_windows_gateways(output) == ["192.168.1.1"]

    def test_no_gateways_yields_empty(self):
        assert parse_windows_gateways("Windows IP Configuration") == []


class TestRenderSummary:
    def test_lists_firmware_defects_before_working_endpoints(self):
        """A 500 buried under forty 200s is a 500 nobody reads."""
        report = make_report(
            reads=[
                ProbeResult("GET", "/system/format", 200),
                ProbeResult("GET", "/mounts/A001-sd1/Stills/", 500),
            ]
        )
        summary = render_summary(report)
        assert summary.index("Stills") < summary.index("/system/format")

    def test_names_the_camera_and_transport(self):
        summary = render_summary(make_report())
        assert "POCKET_6K_G2 v8.6" in summary
        assert "usb" in summary

    def test_reports_failed_subscriptions(self):
        report = make_report(
            websocket={
                "subscriptions": {
                    "/media/active": {"success": True},
                    "/system/videoFormat": {"success": False},
                }
            }
        )
        summary = render_summary(report)
        assert "subscribed 1/2" in summary
        assert "FAILED  /system/videoFormat" in summary

    def test_reports_a_websocket_that_never_connected(self):
        report = make_report(websocket={"error": "ClientError: refused", "subscriptions": {}})
        assert "could not connect" in render_summary(report)

    def test_includes_write_probe_results(self):
        report = make_report(writes=[ProbeResult("PUT", "/system/format", 204)])
        assert "PUT  /system/format" in render_summary(report)


class TestBuildRestProfileBlock:
    def test_marks_working_endpoints_supported(self):
        report = make_report(reads=[ProbeResult("GET", "/system/format", 200)])
        block = build_rest_profile_block(report)
        assert block["endpoints"]["/system/format"] == {"status": 200, "supported": True}

    def test_marks_501_endpoints_unsupported(self):
        report = make_report(reads=[ProbeResult("GET", "/system/videoFormat", 501)])
        block = build_rest_profile_block(report)
        assert block["endpoints"]["/system/videoFormat"]["supported"] is False

    def test_marks_5xx_endpoints_unsupported(self):
        """Reachable but broken must not read as usable — that is the Stills
        defect, and a caller treating it as transient would retry forever."""
        report = make_report(mounts=[ProbeResult("GET", "/mounts/A001-sd1/Stills/", 500)])
        block = build_rest_profile_block(report)
        assert block["endpoints"]["/mounts/A001-sd1/Stills/"]["supported"] is False

    def test_records_put_results_alongside_the_get(self):
        report = make_report(
            probe_writes=True,
            reads=[ProbeResult("GET", "/system/format", 200)],
            writes=[ProbeResult("PUT", "/system/format", 204)],
        )
        entry = build_rest_profile_block(report)["endpoints"]["/system/format"]
        assert entry["status"] == 200
        assert entry["put_status"] == 204
        assert entry["put_supported"] is True

    def test_records_only_successful_subscriptions(self):
        report = make_report(
            websocket={
                "subscriptions": {
                    "/media/active": {"success": True},
                    "/system/videoFormat": {"success": False},
                }
            }
        )
        block = build_rest_profile_block(report)
        assert block["websocket_properties"] == ["/media/active"]

    def test_format_names_is_left_empty_for_a_human(self):
        """Design principle 1: REST codec strings are protocol values and
        must come from the camera, not from a naming rule this tool guessed."""
        assert build_rest_profile_block(make_report())["format_names"] == {}

    def test_provenance_is_candidate_not_verified(self):
        block = build_rest_profile_block(make_report())
        assert block["provenance"]["status"] == "CANDIDATE"
        assert block["_meta"]["status"] == "UNVERIFIED"

    def test_provenance_records_the_transport_and_scope_caveat(self):
        block = build_rest_profile_block(make_report(transport="usb"))
        assert "usb" in block["provenance"]["method"]
        assert "this camera and firmware only" in block["provenance"]["notes"]

    def test_meta_identifies_the_camera(self):
        block = build_rest_profile_block(make_report())
        assert block["_meta"]["model_key"] == "POCKET_6K_G2"
        assert block["_meta"]["firmware"] == "v8.6"

    def test_unreachable_endpoints_carry_the_error_as_a_note(self):
        report = make_report(reads=[ProbeResult("GET", "/system", None, error="OSError: no route")])
        entry = build_rest_profile_block(report)["endpoints"]["/system"]
        assert entry["supported"] is False
        assert "no route" in entry["notes"]


class TestReportSerialisation:
    def test_round_trips_to_a_json_safe_dict(self):
        report = make_report(reads=[ProbeResult("GET", "/system/format", 200, 12.34, {"iso": 400})])
        payload = report.to_dict()
        assert payload["model_key"] == "POCKET_6K_G2"
        assert payload["transport"] == "usb"
        assert payload["reads"][0]["latency_ms"] == 12.3
        assert payload["reads"][0]["classification"] == "ok"

    def test_carries_the_same_value_put_caveat(self):
        assert "not that a changing write applies" in make_report().to_dict()["_caveat"]
