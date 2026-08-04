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
all (`docs/rest/transport.md`'s mounts walk). `get()` defaults
`api_prefixed=True` for every control-API call; callers addressing
`/mounts/...` (`RestCameraSession.list_mount()`/`mount_names()`) must pass
`api_prefixed=False`, or the request 404s against
`/control/api/v1/mounts/...` — a path that was never real.

Every log line is prefixed `[<host>]`, mirroring CLAUDE.md's
`[<ble_name> @ <address>]` convention for the BLE transport.
"""

from __future__ import annotations

import logging
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover - exercised only without the dependency
    # Deliberately not fatal at import time — mirrors
    # tools/rest/probe_endpoints.py's require_aiohttp() pattern, so importing
    # this module never hard-fails in an environment that only wants the
    # pure/offline parts of bmd_camera.
    aiohttp = None  # type: ignore[assignment]

from .constants import API_BASE, DEFAULT_TIMEOUT_S
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

    async def delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

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
