"""Unit tests for :mod:`bmd_camera.rest.client`.

No real network — every test injects a fake aiohttp session, mirroring how
tests/unit/test_camera_controller.py injects a fake BleakClient. Coverage:
the RestClient status-handling contract (204/501/2xx/other), base URL
construction, and session lifecycle (owned vs. injected).
"""

from __future__ import annotations

import aiohttp
import pytest

from bmd_camera.rest.client import RestClient
from bmd_camera.rest.exceptions import BMDConnectionError, BMDRestError, BMDUnsupportedError


class FakeResponse:
    """Stands in for `aiohttp.ClientResponse` as an async context manager."""

    def __init__(self, status: int, *, json_body=None, text_body: str | None = None):
        self.status = status
        self._json_body = json_body
        self._text_body = text_body
        self.content_length = 0 if json_body is None and not text_body else 1

    async def json(self):
        if self._json_body is None:
            raise ValueError("no JSON body")
        return self._json_body

    async def text(self):
        return self._text_body or ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    """Stands in for `aiohttp.ClientSession`. `request()` returns the
    canned response (or raises the canned error) synchronously — matching
    how `RestClient._request` uses it as `async with self._session.request(...)`."""

    def __init__(self, response: FakeResponse | None = None, *, raises: Exception | None = None):
        self.response = response
        self.raises = raises
        self.calls: list[dict] = []
        self.closed = False

    def request(self, method, url, *, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json})
        if self.raises is not None:
            raise self.raises
        return self.response

    async def close(self):
        self.closed = True


class TestBaseUrl:
    def test_no_port(self):
        client = RestClient("pocket-cinema-camera-6k-g2.local", session=FakeSession())
        assert client.base_url == "http://pocket-cinema-camera-6k-g2.local"

    def test_with_port(self):
        client = RestClient("192.168.1.5", port=8080, session=FakeSession())
        assert client.base_url == "http://192.168.1.5:8080"

    def test_https_scheme(self):
        client = RestClient("cam.local", scheme="https", session=FakeSession())
        assert client.base_url == "https://cam.local"


class TestStatusHandling:
    @pytest.mark.asyncio
    async def test_204_returns_none(self):
        session = FakeSession(FakeResponse(204))
        client = RestClient("cam.local", session=session)

        result = await client.get("/system")

        assert result is None
        assert session.calls[0]["method"] == "GET"
        assert session.calls[0]["url"] == "http://cam.local/control/api/v1/system"

    @pytest.mark.asyncio
    async def test_api_prefixed_false_omits_api_base(self):
        """/mounts/... is the Web Media Manager, a separate URL namespace
        from /control/api/v1 (docs/rest/transport.md; confirmed by
        probe_endpoints.py's walk_mounts() building requests without
        API_BASE). Regression test for the real-hardware 404 hit when
        RestClient always prepended API_BASE regardless of destination."""
        session = FakeSession(
            FakeResponse(200, json_body=[{"name": "A001-sd1", "type": "directory"}])
        )
        client = RestClient("cam.local", session=session)

        await client.get("/mounts/", api_prefixed=False)

        assert session.calls[0]["url"] == "http://cam.local/mounts/"

    @pytest.mark.asyncio
    async def test_api_prefixed_true_is_the_default(self):
        session = FakeSession(FakeResponse(200, json_body={"codec": "ProRes:HQ"}))
        client = RestClient("cam.local", session=session)

        await client.get("/system/format")

        assert session.calls[0]["url"] == "http://cam.local/control/api/v1/system/format"

    @pytest.mark.asyncio
    async def test_200_returns_parsed_json_body(self):
        body = {"codec": "ProRes:HQ", "frameRate": "24"}
        session = FakeSession(FakeResponse(200, json_body=body))
        client = RestClient("cam.local", session=session)

        result = await client.get("/system/format")

        assert result == body

    @pytest.mark.asyncio
    async def test_200_with_empty_body_returns_none(self):
        session = FakeSession(FakeResponse(200))
        client = RestClient("cam.local", session=session)

        result = await client.get("/transports/0/stop")

        assert result is None

    @pytest.mark.asyncio
    async def test_501_raises_bmd_unsupported_error(self):
        session = FakeSession(FakeResponse(501, json_body={"error": "Not implemented"}))
        client = RestClient("cam.local", session=session)

        with pytest.raises(BMDUnsupportedError, match="501"):
            await client.get("/system/videoFormat")

    @pytest.mark.asyncio
    async def test_404_raises_bmd_rest_error(self):
        session = FakeSession(FakeResponse(404, json_body={"error": "not found"}))
        client = RestClient("cam.local", session=session)

        with pytest.raises(BMDRestError, match="404"):
            await client.get("/audio/channel/2/input")

    @pytest.mark.asyncio
    async def test_404_error_carries_structured_status_and_body(self):
        """BMDRestError.status/.body let a caller with camera-semantic
        context (e.g. RestCameraSession.clips()) interpret a specific
        response meaningfully without string-matching the message —
        see tests/unit/rest/test_rest_session.py's clips()-with-no-media
        translation to BMDStorageError."""
        session = FakeSession(FakeResponse(404, json_body={"error": "No disk or media"}))
        client = RestClient("cam.local", session=session)

        with pytest.raises(BMDRestError) as exc_info:
            await client.get("/clips/list")

        assert exc_info.value.status == 404
        assert exc_info.value.body == {"error": "No disk or media"}

    @pytest.mark.asyncio
    async def test_500_raises_bmd_rest_error_not_unsupported(self):
        """A 500 is a firmware defect (docs/rest/transport.md's mounts bug),
        distinct from a 501's "correctly declining" — must not be conflated
        with BMDUnsupportedError."""
        session = FakeSession(FakeResponse(500, json_body={"error": "server error"}))
        client = RestClient("cam.local", session=session)

        with pytest.raises(BMDRestError, match="500"):
            await client.get("/mounts/A001-sd1/Stills/")

    @pytest.mark.asyncio
    async def test_connection_error_raises_bmd_connection_error(self):
        session = FakeSession(raises=aiohttp.ClientConnectionError("refused"))
        client = RestClient("cam.local", session=session)

        with pytest.raises(BMDConnectionError, match="cam.local"):
            await client.get("/system")

    @pytest.mark.asyncio
    async def test_no_session_raises_bmd_connection_error(self):
        client = RestClient("cam.local", session=None)
        client._session = None  # never entered the async context manager

        with pytest.raises(BMDConnectionError, match="no open session"):
            await client.get("/system")


class TestExists:
    """exists() never touches the response body — see its docstring for
    why a plain get() risks a decode error on a real binary still file.
    FakeResponse(200) here carries no json_body/text_body on purpose: if
    exists() ever regressed to calling .json()/.text(), FakeResponse.json()
    would raise ValueError("no JSON body"), failing these tests loudly."""

    @pytest.mark.asyncio
    async def test_2xx_returns_true_without_reading_body(self):
        session = FakeSession(FakeResponse(200))
        client = RestClient("cam.local", session=session)

        assert await client.exists("/mounts/A001-sd1/Stills/A001_0001_S001.dng") is True

    @pytest.mark.asyncio
    async def test_api_prefixed_false_omits_api_base(self):
        """Same /mounts/ namespace concern as TestStatusHandling's
        test_api_prefixed_false_omits_api_base — exists() is the primitive
        rest/media.py's still-file probing uses against /mounts/... paths."""
        session = FakeSession(FakeResponse(200))
        client = RestClient("cam.local", session=session)

        await client.exists("/mounts/A001-sd1/Stills/A001_0001_S001.dng", api_prefixed=False)

        assert (
            session.calls[0]["url"] == "http://cam.local/mounts/A001-sd1/Stills/A001_0001_S001.dng"
        )

    @pytest.mark.asyncio
    async def test_204_returns_true(self):
        session = FakeSession(FakeResponse(204))
        client = RestClient("cam.local", session=session)

        assert await client.exists("/system/format") is True

    @pytest.mark.asyncio
    async def test_404_returns_false(self):
        session = FakeSession(FakeResponse(404))
        client = RestClient("cam.local", session=session)

        assert await client.exists("/mounts/A001-sd1/Stills/A001_0001_S999.dng") is False

    @pytest.mark.asyncio
    async def test_501_raises_bmd_unsupported_error(self):
        session = FakeSession(FakeResponse(501))
        client = RestClient("cam.local", session=session)

        with pytest.raises(BMDUnsupportedError, match="501"):
            await client.exists("/system/videoFormat")

    @pytest.mark.asyncio
    async def test_500_raises_bmd_rest_error(self):
        session = FakeSession(FakeResponse(500))
        client = RestClient("cam.local", session=session)

        with pytest.raises(BMDRestError, match="500"):
            await client.exists("/mounts/A001-sd1/Stills/A001_0001_S001.dng")

    @pytest.mark.asyncio
    async def test_no_session_raises_bmd_connection_error(self):
        client = RestClient("cam.local", session=None)
        client._session = None

        with pytest.raises(BMDConnectionError, match="no open session"):
            await client.exists("/system")

    @pytest.mark.asyncio
    async def test_connection_error_raises_bmd_connection_error(self):
        session = FakeSession(raises=aiohttp.ClientConnectionError("refused"))
        client = RestClient("cam.local", session=session)

        with pytest.raises(BMDConnectionError, match="cam.local"):
            await client.exists("/system")


class TestWriteVerbs:
    @pytest.mark.asyncio
    async def test_put_sends_json_body(self):
        session = FakeSession(FakeResponse(204))
        client = RestClient("cam.local", session=session)

        result = await client.put("/video/iso", {"iso": 800})

        assert result is None
        assert session.calls[0]["method"] == "PUT"
        assert session.calls[0]["json"] == {"iso": 800}

    @pytest.mark.asyncio
    async def test_post_and_delete(self):
        session = FakeSession(FakeResponse(204))
        client = RestClient("cam.local", session=session)

        await client.post("/timelines/0/add", {"clipUniqueId": 1})
        await client.delete("/timelines/0")

        assert [c["method"] for c in session.calls] == ["POST", "DELETE"]


class TestSessionLifecycle:
    @pytest.mark.asyncio
    async def test_owned_session_closed_on_exit(self):
        client = RestClient("cam.local")
        async with client as entered:
            assert entered._session is not None
        assert client._session is None

    @pytest.mark.asyncio
    async def test_injected_session_not_closed_on_exit(self):
        session = FakeSession(FakeResponse(204))
        client = RestClient("cam.local", session=session)

        async with client:
            pass

        assert session.closed is False
