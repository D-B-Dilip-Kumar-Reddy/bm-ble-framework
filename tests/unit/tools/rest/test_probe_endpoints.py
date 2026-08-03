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
    alternate_scheme_url,
    build_catalog,
    build_rest_profile_block,
    classify,
    device_names_from_workingset,
    diagnose,
    echo_body,
    expand_catalog,
    extract_links,
    is_response_to,
    is_supported,
    normalise_base_url,
    pick_first,
    render_summary,
    subscribable_properties,
    transport_mode_body,
    websocket_url,
    ws_response_ok,
)


def make_report(**overrides) -> Report:
    defaults = {
        "model_key": "POCKET_6K_G2",
        "firmware": "v8.6",
        "transport": "usb",
        "host": "https://10.0.0.3",
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

    def test_skips_empty_slots(self):
        """The working set is a fixed size with empty members reporting
        deviceName "". Expanding those produced requests to
        `/media/devices/` and `/media/devices//doformat`, whose 404s say
        nothing about the camera."""
        payload = {
            "workingset": [
                {"index": 0, "deviceName": ""},
                {"index": 1, "deviceName": "sd0", "activeDisk": True},
                {"index": 2, "deviceName": ""},
            ]
        }
        assert device_names_from_workingset(payload) == ["sd0"]

    def test_active_device_is_not_assumed_to_be_slot_zero(self):
        """Real hardware: three slots, only index 1 populated. Indexing slot
        0 would find an empty device."""
        payload = {
            "workingset": [
                {"index": 0, "deviceName": ""},
                {"index": 1, "deviceName": "sd0", "activeDisk": True},
                {"index": 2, "deviceName": ""},
            ]
        }
        assert device_names_from_workingset(payload)[0] == "sd0"


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


class TestIsResponseTo:
    """Regression tests for the first sweep's silent off-by-one. The camera
    interleaves events with responses, so "the next message" is not "my
    answer" — reading positionally credited every property with the previous
    one's outcome, and nothing in the output looked wrong."""

    def test_matches_the_response_with_the_same_id(self):
        assert is_response_to({"type": "response", "id": 7, "data": {}}, 7) is True

    def test_rejects_a_response_to_a_different_request(self):
        assert is_response_to({"type": "response", "id": 6, "data": {}}, 7) is False

    def test_rejects_the_websocket_opened_event(self):
        """The exact message that consumed request id=0's slot in the first
        real run."""
        opened = {"data": {"action": "websocketOpened"}, "type": "event"}
        assert is_response_to(opened, 0) is False

    def test_rejects_a_property_value_changed_event(self):
        event = {
            "type": "event",
            "data": {"action": "propertyValueChanged", "property": "/video/iso"},
        }
        assert is_response_to(event, 0) is False

    @pytest.mark.parametrize("message", [None, {}, "text", {"type": "response"}])
    def test_rejects_malformed_or_id_less_messages(self, message):
        assert is_response_to(message, 0) is False


class TestExtractLinks:
    def test_reads_the_json_listing_the_camera_actually_serves(self):
        """POCKET_6K_G2 v8.6 returns JSON, not HTML. Assuming HTML meant the
        first sweep never descended past /mounts/, so the known Stills 500
        was neither reproduced nor disproved."""
        body = [{"name": "A001-sd1", "type": "directory", "mtime": "Mon, 31 Dec 1979 05:30:00"}]
        assert extract_links(body) == ["A001-sd1"]

    def test_json_listing_skips_files(self):
        body = [
            {"name": "A001-sd1", "type": "directory"},
            {"name": "clip.mov", "type": "file"},
        ]
        assert extract_links(body) == ["A001-sd1"]

    def test_json_listing_tolerates_malformed_entries(self):
        body = [{"type": "directory"}, {"name": 7, "type": "directory"}, "junk"]
        assert extract_links(body) == []

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


class TestDiagnose:
    """A failed preflight must explain itself. These assertions exist because
    an early version told the operator to add --insecure for a refused
    connection: aiohttp puts "ssl:default" in ordinary ClientConnectorError
    messages, and a substring match on "ssl" caught it."""

    CERT = "ClientConnectorCertificateError: certificate verify failed: self-signed certificate"
    REFUSED = (
        "ClientConnectorError: Cannot connect to host 10.0.0.3:443 ssl:default "
        "[The remote computer refused the network connection]"
    )
    TIMEOUT = "TimeoutError: "
    DNS = "ClientConnectorError: [Errno -2] Name or service not known"

    def test_no_error_means_no_advice(self):
        assert diagnose(None) == []

    def test_certificate_failure_recommends_insecure(self):
        assert any("--insecure" in step for step in diagnose(self.CERT))

    def test_refused_connection_does_not_blame_tls(self):
        steps = diagnose(self.REFUSED)
        assert not any("--insecure" in step for step in steps)
        assert any("refused or had no route" in step for step in steps)
        assert any("Network Access" in step for step in steps)

    def test_timeout_suggests_a_wrong_or_dropped_address(self):
        steps = diagnose(self.TIMEOUT)
        assert any("hung rather than being refused" in step for step in steps)
        assert not any("--insecure" in step for step in steps)

    def test_dns_failure_is_named(self):
        assert any("did not resolve" in step for step in diagnose(self.DNS))

    @pytest.mark.parametrize("error", [CERT, REFUSED, TIMEOUT, DNS])
    def test_every_failure_suggests_confirming_the_browser_still_works(self, error):
        """The link drops on sleep and unplug, so 'it worked earlier' is not
        evidence that it works now."""
        assert any("RIGHT NOW" in step for step in diagnose(error))

    @pytest.mark.parametrize("error", [CERT, REFUSED, TIMEOUT, DNS])
    def test_every_failure_offers_an_independent_cross_check(self, error):
        assert any("curl" in step for step in diagnose(error))


class TestNormaliseBaseUrl:
    def test_defaults_to_http(self):
        """Matches what Setup -> Network Access advertises: "Web media
        manager (HTTP): Enabled" serves plaintext on :80. Port 443 refused
        for both this tool and curl on 2026-08-03."""
        assert normalise_base_url("172.30.161.225") == "http://172.30.161.225"

    def test_accepts_an_mdns_name(self):
        assert (
            normalise_base_url("pocket-cinema-camera-6k-g2.local")
            == "http://pocket-cinema-camera-6k-g2.local"
        )

    @pytest.mark.parametrize("url", ["http://172.30.161.225", "https://172.30.161.225"])
    def test_a_full_url_overrides_the_scheme(self, url):
        assert normalise_base_url(url, "https") == url

    def test_strips_trailing_slash_and_whitespace(self):
        assert normalise_base_url("  172.30.161.225/  ") == "http://172.30.161.225"

    def test_explicit_scheme_argument_is_honoured(self):
        assert normalise_base_url("172.30.161.225", "https") == "https://172.30.161.225"


class TestAlternateSchemeUrl:
    """The preflight retries with the other scheme, because which listener is
    live depends on the camera's "Web media manager" setting: "Enabled"
    serves plaintext on :80, "Enabled with security only" serves TLS on :443."""

    def test_http_becomes_https(self):
        assert alternate_scheme_url("http://cam.local") == "https://cam.local"

    def test_https_becomes_http(self):
        assert alternate_scheme_url("https://cam.local") == "http://cam.local"

    def test_round_trips(self):
        assert alternate_scheme_url(alternate_scheme_url("http://cam.local")) == "http://cam.local"

    def test_preserves_path_and_port(self):
        assert alternate_scheme_url("http://172.30.161.225:8080") == "https://172.30.161.225:8080"

    def test_rejects_a_schemeless_url(self):
        with pytest.raises(ValueError, match="http:// or https://"):
            alternate_scheme_url("172.30.161.225")


class TestWebsocketUrl:
    def test_https_becomes_wss_not_ws(self):
        """Downgrading here would silently fail against a camera that only
        serves the secure listener."""
        assert websocket_url("https://10.0.0.3") == "wss://10.0.0.3/control/api/v1/event/websocket"

    def test_http_becomes_ws(self):
        assert websocket_url("http://10.0.0.3") == "ws://10.0.0.3/control/api/v1/event/websocket"

    def test_preserves_an_mdns_host(self):
        assert websocket_url("https://pocket-cinema-camera-6k-g2.local").startswith(
            "wss://pocket-cinema-camera-6k-g2.local/"
        )

    def test_rejects_a_schemeless_base_url(self):
        with pytest.raises(ValueError, match="http:// or https://"):
            websocket_url("10.0.0.3")


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
