"""Unit tests for tools/rest/smoke_test_client.py — pure endpoint-selection
logic and URL helpers only. No HTTP, no camera required.

tools/ is not a package, so tools/rest is added to sys.path directly (same
pattern as tests/unit/tools/rest/test_probe_endpoints.py).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tools" / "rest"))

from smoke_test_client import (  # noqa: E402
    normalise_base_url,
    pick_not_implemented_endpoint,
    pick_readable_endpoint,
    pick_write_verify_property,
    websocket_url,
)

from bmd_camera.camera_profile import RestEndpointSpec  # noqa: E402


def _spec(**overrides) -> RestEndpointSpec:
    defaults = {"path": "/x", "status": 200, "supported": True}
    defaults.update(overrides)
    return RestEndpointSpec(**defaults)


class TestNormaliseBaseUrl:
    def test_bare_host_gets_scheme_prefixed(self):
        assert normalise_base_url("cam.local", "http") == "http://cam.local"

    def test_host_with_scheme_wins_over_scheme_arg(self):
        assert normalise_base_url("https://cam.local", "http") == "https://cam.local"

    def test_strips_trailing_slash(self):
        assert normalise_base_url("cam.local/", "http") == "http://cam.local"


class TestWebsocketUrl:
    def test_http_becomes_ws(self):
        assert websocket_url("http://cam.local") == "ws://cam.local/control/api/v1/event/websocket"

    def test_https_becomes_wss(self):
        assert (
            websocket_url("https://cam.local") == "wss://cam.local/control/api/v1/event/websocket"
        )

    def test_rejects_schemeless_base_url(self):
        try:
            websocket_url("cam.local")
        except ValueError as exc:
            assert "http://" in str(exc)
        else:
            raise AssertionError("expected ValueError")


class TestPickReadableEndpoint:
    def test_picks_first_confirmed_200_alphabetically(self):
        endpoints = {
            "/video/iso": _spec(path="/video/iso", status=200, supported=True),
            "/audio/channel/0/level": _spec(
                path="/audio/channel/0/level", status=200, supported=True
            ),
        }
        assert pick_readable_endpoint(endpoints) == "/audio/channel/0/level"

    def test_excludes_given_paths(self):
        endpoints = {"/system": _spec(path="/system", status=204, supported=True)}
        assert pick_readable_endpoint(endpoints, exclude=frozenset({"/system"})) is None

    def test_skips_unsupported_and_non_200(self):
        endpoints = {
            "/system": _spec(path="/system", status=204, supported=True),
            "/system/videoFormat": _spec(path="/system/videoFormat", status=501, supported=False),
        }
        assert pick_readable_endpoint(endpoints) is None

    def test_returns_none_for_empty_endpoints(self):
        assert pick_readable_endpoint({}) is None


class TestPickNotImplementedEndpoint:
    def test_picks_first_501_alphabetically(self):
        endpoints = {
            "/system/videoFormat": _spec(path="/system/videoFormat", status=501, supported=False),
            "/system/codecFormat": _spec(path="/system/codecFormat", status=501, supported=False),
        }
        assert pick_not_implemented_endpoint(endpoints) == "/system/codecFormat"

    def test_returns_none_when_nothing_is_501(self):
        endpoints = {"/system/format": _spec(path="/system/format", status=200, supported=True)}
        assert pick_not_implemented_endpoint(endpoints) is None


class TestPickWriteVerifyProperty:
    def test_requires_both_put_supported_and_websocket_membership(self):
        endpoints = {
            "/video/iso": _spec(path="/video/iso", status=200, supported=True, put_supported=True),
            "/lens/focus": _spec(
                path="/lens/focus", status=200, supported=True, put_supported=True
            ),
        }
        # Only /lens/focus is a confirmed websocket_properties member.
        result = pick_write_verify_property(endpoints, ("/lens/focus",))
        assert result == "/lens/focus"

    def test_returns_none_when_put_supported_but_not_subscribable(self):
        endpoints = {
            "/video/iso": _spec(path="/video/iso", status=200, supported=True, put_supported=True)
        }
        assert pick_write_verify_property(endpoints, ()) is None

    def test_returns_none_when_subscribable_but_not_put_supported(self):
        endpoints = {
            "/clips/list": _spec(path="/clips/list", status=200, supported=True, put_supported=None)
        }
        assert pick_write_verify_property(endpoints, ("/clips/list",)) is None

    def test_deterministic_alphabetical_pick(self):
        endpoints = {
            "/video/iso": _spec(path="/video/iso", status=200, supported=True, put_supported=True),
            "/audio/channel/0/level": _spec(
                path="/audio/channel/0/level", status=200, supported=True, put_supported=True
            ),
        }
        ws_props = ("/video/iso", "/audio/channel/0/level")
        assert pick_write_verify_property(endpoints, ws_props) == "/audio/channel/0/level"
