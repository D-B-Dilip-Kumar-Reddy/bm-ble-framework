"""
bmd_camera/rest/client.py
===========================
RestClient — REST transport layer for the Blackmagic Camera 8.6 control API.
No BMD/camera semantics beyond HTTP status-code interpretation: this holds
the same transport-only boundary design principle 5 draws for
camera_controller.py on the BLE side.

STATUS-HANDLING CONTRACT
──────────────────────────
A naive `raise_for_status()` gets this camera wrong on two counts confirmed
by the Phase 0 sweep (docs/rest/transport.md): `GET /system` legitimately
returns 204 with no body, and five endpoints legitimately return 501 rather
than raising. So:

  - 2xx  -> the parsed JSON body, or None for an empty body (204 or empty 200)
  - 501  -> raises BMDUnsupportedError — the camera is correctly declining,
            not failing (design principle 7)
  - other non-2xx (4xx/5xx) -> raises BMDRestError
  - connection failure / timeout -> raises BMDConnectionError

TWO URL NAMESPACES ON ONE HOST
─────────────────────────────────
`/control/api/v1/...` (the control API `API_BASE` covers) is not the whole
HTTP surface this camera serves. `/mounts/...` is the Web Media Manager —
a separate namespace at the host root, confirmed by
`tools/rest/probe_endpoints.py`'s own two request-building call sites:
the endpoint catalog sweep builds `f"{base_url}{API_BASE}{endpoint.path}"`
while `walk_mounts()` builds `f"{base_url}{path}"` with no `API_BASE` at
all (`docs/rest/transport.md`'s mounts walk). `get()`/`exists()` default
`api_prefixed=True` for every control-API call; callers addressing
`/mounts/...` (`RestCameraSession.list_mount()`/`mount_names()`/
`path_exists()`) must pass `api_prefixed=False`, or the request 404s
against `/control/api/v1/mounts/...` — a path that was never real.

Every log line is prefixed `[<host>]`, mirroring CLAUDE.md's
`[<ble_name> @ <address>]` convention for the BLE transport.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover - exercised only without the dependency
    # Deliberately not fatal at import time — mirrors
    # tools/rest/probe_endpoints.py's require_aiohttp() pattern, so importing
    # this module never hard-fails in an environment that only wants the
    # pure/offline parts of bmd_camera.
    aiohttp = None  # type: ignore[assignment]

from .constants import API_BASE, DEFAULT_DOWNLOAD_STALL_TIMEOUT_S, DEFAULT_TIMEOUT_S
from .exceptions import BMDConnectionError, BMDRestError, BMDUnsupportedError

logger = logging.getLogger(__name__)


def require_aiohttp() -> None:
    """Raise a useful error if the HTTP dependency is missing."""
    if aiohttp is None:
        raise RuntimeError(
            "aiohttp is required to talk to the camera over REST. Install it with:\n"
            "    python -m pip install -r requirements.txt"
        )


class RestClient:
    """Async REST transport for one camera's control API.

    Usage:
        async with RestClient("pocket-cinema-camera-6k-g2.local") as client:
            fmt = await client.get("/system/format")
            await client.put("/system/format", fmt)

    `session` may be injected (real or fake) for testing, mirroring how
    camera_controller.py's tests inject a fake BleakClient — see
    tests/unit/rest/test_client.py.
    """

    def __init__(
        self,
        host: str,
        *,
        scheme: str = "http",
        port: int | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        session: Any | None = None,
    ) -> None:
        require_aiohttp()
        self.host = host
        self.scheme = scheme
        self.port = port
        self.timeout_s = timeout_s
        self._session = session
        self._owns_session = session is None
        self._log = logging.getLogger(f"{__name__}.{host}")

    @property
    def base_url(self) -> str:
        netloc = f"{self.host}:{self.port}" if self.port else self.host
        return f"{self.scheme}://{netloc}"

    async def __aenter__(self) -> RestClient:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def get(self, path: str, *, api_prefixed: bool = True) -> Any:
        return await self._request("GET", path, api_prefixed=api_prefixed)

    async def put(self, path: str, body: Any) -> Any:
        return await self._request("PUT", path, json_body=body)

    async def post(self, path: str, body: Any = None) -> Any:
        return await self._request("POST", path, json_body=body)

    async def delete(self, path: str, *, api_prefixed: bool = True) -> Any:
        return await self._request("DELETE", path, api_prefixed=api_prefixed)

    async def exists(self, path: str, *, api_prefixed: bool = True) -> bool:
        """Whether `GET path` returns a successful status, without ever
        attempting to parse or decode the response body.

        `get()`'s body handling (`_read_body`) tries `.json()` then falls
        back to `.text()` — fine for every endpoint this client has talked
        to so far, all of which return JSON or empty bodies, but a real
        still image (`.dng`/`.braw`) is binary content `.text()` could
        raise decoding on. `exists()` exists specifically for probing
        whether a media file is present under `/mounts/...`
        (`rest/media.py`'s `guess_new_still_path()`) without ever touching
        its content.

        `/mounts/...` paths live outside `API_BASE` — pass
        `api_prefixed=False` for them, per this module's docstring.

        Returns `True` for `2xx`, `False` for `404`. Still raises
        `BMDUnsupportedError` for `501` and `BMDRestError` for any other
        non-2xx — the same status contract `get()` has, minus the body.
        """
        if self._session is None:
            raise BMDConnectionError(
                "RestClient has no open session — use 'async with RestClient(...)' "
                "or pass an existing session to the constructor"
            )
        url = self._url(path, api_prefixed=api_prefixed)
        self._log.debug("[%s] HEAD-style GET %s", self.host, path)
        try:
            async with self._session.request(
                "GET", url, timeout=aiohttp.ClientTimeout(total=self.timeout_s)
            ) as resp:
                status = resp.status
        except aiohttp.ClientError as exc:
            raise BMDConnectionError(f"[{self.host}] GET {path} failed: {exc}") from exc
        except TimeoutError as exc:
            raise BMDConnectionError(
                f"[{self.host}] GET {path} timed out after {self.timeout_s}s"
            ) from exc

        self._log.debug("[%s] GET %s -> %s", self.host, path, status)

        if status == 404:
            return False
        if status == 501:
            raise BMDUnsupportedError(f"[{self.host}] GET {path} — not implemented (501)")
        if 200 <= status < 300:
            return True
        raise BMDRestError(f"[{self.host}] GET {path} -> {status}", status=status, body=None)

    async def download(
        self,
        path: str,
        dest: str | Path,
        *,
        api_prefixed: bool = False,
        chunk_size: int = 1024 * 1024,
        stall_timeout_s: float = DEFAULT_DOWNLOAD_STALL_TIMEOUT_S,
    ) -> int:
        """Stream `GET path`'s response body straight to `dest` on local
        disk, never holding the whole file in memory — a real clip runs
        tens of GB (`docs/rest/session.md`'s `rest_record_test_clip.py`
        real-hardware runs), and `get()`'s `.json()`/`.text()` body
        handling would either raise decoding binary content (the exact
        crash class `exists()` was built to avoid — see its docstring) or
        hold the entire file in memory trying not to.

        Reads via `aiohttp`'s streaming `resp.content.iter_chunked()`;
        each chunk is written with `asyncio.to_thread()` so a disk write
        never blocks the event loop (design principle 11). `dest`'s
        parent directory must already exist — this method does not create
        directories, matching `format_device()`/`delete_clip()`'s "do
        exactly what was asked, nothing implicit" discipline.

        **Timeout is stall-based, not total-based, unlike every other
        method here.** `DEFAULT_TIMEOUT_S` (5s) is a sane budget for a
        JSON control-API round trip; it is nonsensical for a multi-GB
        transfer, where the *total* time is expected to be long but any
        single gap in received data past `stall_timeout_s` (default 30s)
        means the transfer has genuinely stalled. Implemented via
        `aiohttp.ClientTimeout(sock_connect=self.timeout_s,
        sock_read=stall_timeout_s)` — no `total` cap at all.

        `/mounts/...` paths live outside `API_BASE` — unlike `get()`/
        `exists()`/`delete()`, `api_prefixed` defaults to `False` here,
        since every real caller downloads a real media file from the Web
        Media Manager namespace, never a control-API JSON response.

        Returns the number of bytes written. Raises `BMDRestError` if the
        server's own `Content-Length` header doesn't match what was
        actually received — a transport-level integrity check (design
        principle 5's boundary: this is raw HTTP correctness, not camera
        semantics). `RestCameraSession`'s own `download_clip()`/
        `download_still()` layer an `exists()`-before check on top of
        this, the same verification shape `delete_clip()`/`delete_still()`
        use for the mirror-image operation.
        """
        if self._session is None:
            raise BMDConnectionError(
                "RestClient has no open session — use 'async with RestClient(...)' "
                "or pass an existing session to the constructor"
            )
        url = self._url(path, api_prefixed=api_prefixed)
        dest = Path(dest)
        self._log.debug("[%s] GET %s -> %s (streaming)", self.host, path, dest)
        timeout = aiohttp.ClientTimeout(sock_connect=self.timeout_s, sock_read=stall_timeout_s)
        try:
            async with self._session.request("GET", url, timeout=timeout) as resp:
                status = resp.status
                if status == 501:
                    raise BMDUnsupportedError(f"[{self.host}] GET {path} — not implemented (501)")
                if not (200 <= status < 300):
                    body = await self._read_body(resp)
                    raise BMDRestError(
                        f"[{self.host}] GET {path} -> {status}: {body!r}",
                        status=status,
                        body=body,
                    )
                expected = resp.content_length
                written = 0
                with dest.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(chunk_size):
                        await asyncio.to_thread(f.write, chunk)
                        written += len(chunk)
        except aiohttp.ClientError as exc:
            raise BMDConnectionError(f"[{self.host}] GET {path} failed: {exc}") from exc
        except TimeoutError as exc:
            raise BMDConnectionError(
                f"[{self.host}] GET {path} stalled (no data for {stall_timeout_s}s)"
            ) from exc

        self._log.debug(
            "[%s] GET %s -> %s, %d bytes written to %s", self.host, path, status, written, dest
        )

        if expected is not None and written != expected:
            raise BMDRestError(
                f"[{self.host}] GET {path}: server reported Content-Length={expected} but "
                f"{written} bytes were actually received and written to {dest}",
                status=status,
                body=None,
            )
        return written

    def _url(self, path: str, *, api_prefixed: bool) -> str:
        prefix = API_BASE if api_prefixed else ""
        return f"{self.base_url}{prefix}{path}"

    async def _request(
        self, method: str, path: str, *, json_body: Any = None, api_prefixed: bool = True
    ) -> Any:
        if self._session is None:
            raise BMDConnectionError(
                "RestClient has no open session — use 'async with RestClient(...)' "
                "or pass an existing session to the constructor"
            )
        url = self._url(path, api_prefixed=api_prefixed)
        self._log.debug("[%s] %s %s", self.host, method, path)
        try:
            async with self._session.request(
                method,
                url,
                json=json_body,
                timeout=aiohttp.ClientTimeout(total=self.timeout_s),
            ) as resp:
                status = resp.status
                body = await self._read_body(resp)
        except aiohttp.ClientError as exc:
            raise BMDConnectionError(f"[{self.host}] {method} {path} failed: {exc}") from exc
        except TimeoutError as exc:
            raise BMDConnectionError(
                f"[{self.host}] {method} {path} timed out after {self.timeout_s}s"
            ) from exc

        self._log.debug("[%s] %s %s -> %s", self.host, method, path, status)

        if status == 501:
            raise BMDUnsupportedError(f"[{self.host}] {method} {path} — not implemented (501)")
        if 200 <= status < 300:
            return body
        raise BMDRestError(
            f"[{self.host}] {method} {path} -> {status}: {body!r}", status=status, body=body
        )

    @staticmethod
    async def _read_body(resp: Any) -> Any:
        if resp.content_length == 0:
            return None
        try:
            return await resp.json()
        except (aiohttp.ContentTypeError, ValueError):
            text = await resp.text()
            return text or None
