"""
tools/rest/smoke_test_client.py
=================================
Exercises `RestClient` and `RestEventRouter.arm()`/`wait_for()` against a
real camera — the two pieces of the Phase 2 library surface that
`tools/rest/watch_events.py` never touches (it only opens the WebSocket and
observes; it never calls `RestClient.get()`/`.put()`, and never arms a
write). See docs/rest/transport.md's "Library surface (Phase 2)" section.

Endpoints are chosen from the target camera/firmware's own `rest/<fw>.json`
profile (`payloads/models/<MODEL_KEY>/rest/<FIRMWARE>.json`) — never
hardcoded — so this tool only ever exercises paths the Phase 0 sweep already
confirmed exist on that exact camera (design principle 6's REST sibling
rule).

TWO MODES
─────────
Read-only (default, always runs): confirms `RestClient`'s full status-code
contract against real responses —

  - `GET /system` -> `None` (the confirmed `204` case)
  - `GET` a confirmed-`200` endpoint -> a parsed body (the `2xx`-with-body case)
  - `GET` a confirmed-`501` endpoint -> raises `BMDUnsupportedError`

`--verify-write` (opt-in, typed-yes gated, matching tools/control/'s
convention): additionally runs one full write-verify round trip — the exact
primary/secondary dual-check pattern design principle 3 specifies for REST
(`RestEventRouter` event primary, `GET` readback secondary) — on one
same-value PUT:

  1. `arm(property)` on `RestEventRouter` (subscribed and buffering first)
  2. `GET` the current value via `RestClient`
  3. `PUT` that same value straight back (idempotent — never a changing write)
  4. `wait_for(property, timeout)` — primary confirmation
  5. `GET` again — secondary confirmation

The property is auto-picked as the first one in the profile that is both
`put_supported` and a confirmed `websocket_properties` member, so `arm()`/
`wait_for()` has something to observe; override with `--property`.

Usage:
    python tools/rest/smoke_test_client.py --host 172.27.97.141 \
        --model-key POCKET_6K_PRO --firmware v8.6

    python tools/rest/smoke_test_client.py --host 172.27.97.141 \
        --model-key POCKET_6K_PRO --firmware v8.6 --verify-write
"""

from __future__ import annotations

import argparse
import asyncio
import logging

try:
    import aiohttp
except ImportError:  # pragma: no cover - exercised only without the dependency
    aiohttp = None  # type: ignore[assignment]

from bmd_camera.camera_profile import CameraProfile, RestEndpointSpec
from bmd_camera.rest.client import RestClient, require_aiohttp
from bmd_camera.rest.constants import WS_PATH
from bmd_camera.rest.events import RestEventRouter
from bmd_camera.rest.exceptions import BMDRestError, BMDUnsupportedError

logger = logging.getLogger(__name__)


def normalise_base_url(host: str, scheme: str) -> str:
    """`host` as a scheme-qualified base URL with no trailing slash. A
    `--host` that already carries a scheme wins over `--scheme`."""
    host = host.strip().rstrip("/")
    if host.startswith(("http://", "https://")):
        return host
    return f"{scheme}://{host}"


def websocket_url(base_url: str) -> str:
    """The event-feed URL for a base URL, preserving TLS (wss for https)."""
    if base_url.startswith("https://"):
        return f"wss://{base_url[len('https://') :]}{WS_PATH}"
    if base_url.startswith("http://"):
        return f"ws://{base_url[len('http://') :]}{WS_PATH}"
    raise ValueError(f"base_url must start with http:// or https:// — got {base_url!r}")


def pick_readable_endpoint(
    endpoints: dict[str, RestEndpointSpec], *, exclude: frozenset[str] = frozenset()
) -> str | None:
    """First endpoint the sweep confirmed as a readable `2xx`-with-body
    (`status == 200`, not the `/system`-style `204`-empty case), for
    exercising `RestClient`'s "returns a parsed body" branch."""
    for path in sorted(endpoints):
        if path in exclude:
            continue
        spec = endpoints[path]
        if spec.status == 200 and spec.supported:
            return path
    return None


def pick_not_implemented_endpoint(endpoints: dict[str, RestEndpointSpec]) -> str | None:
    """First endpoint the sweep confirmed as `501`, for exercising
    `RestClient`'s `BMDUnsupportedError` branch."""
    for path in sorted(endpoints):
        if endpoints[path].status == 501:
            return path
    return None


def pick_write_verify_property(
    endpoints: dict[str, RestEndpointSpec], websocket_properties: tuple[str, ...]
) -> str | None:
    """First endpoint that is both `put_supported` (so a same-value `PUT`
    is confirmed safe) and a confirmed `websocket_properties` member (so
    `arm()`/`wait_for()` has something to observe) — the two preconditions
    a real write-verify round trip needs."""
    ws_props = set(websocket_properties)
    for path in sorted(endpoints):
        spec = endpoints[path]
        if spec.put_supported and path in ws_props:
            return path
    return None


async def check_204_returns_none(client: RestClient) -> None:
    print("\n[1/3] GET /system — expecting None (confirmed 204 on this camera) …")
    result = await client.get("/system")
    if result is None:
        print("  OK — returned None")
    else:
        print(f"  UNEXPECTED — returned {result!r} instead of None")


async def check_200_returns_body(client: RestClient, path: str) -> None:
    print(f"\n[2/3] GET {path} — expecting a parsed body (confirmed 200 on this camera) …")
    result = await client.get(path)
    if result is not None:
        print(f"  OK — returned {result!r}")
    else:
        print("  UNEXPECTED — returned None")


async def check_501_raises_unsupported(client: RestClient, path: str) -> None:
    print(f"\n[3/3] GET {path} — expecting BMDUnsupportedError (confirmed 501 on this camera) …")
    try:
        result = await client.get(path)
    except BMDUnsupportedError as exc:
        print(f"  OK — raised BMDUnsupportedError: {exc}")
    else:
        print(f"  UNEXPECTED — returned {result!r} instead of raising")


async def confirm_write_verify(prop: str, current_value: object) -> bool:
    print(f"\nAbout to PUT the current value straight back to {prop}:")
    print(f"  {current_value!r}")
    print(
        "\nThis is idempotent (same-value only) but is a real write to the camera. "
        "Keep the camera in view."
    )
    loop = asyncio.get_running_loop()
    answer = await loop.run_in_executor(None, input, "Type 'yes' to proceed: ")
    return answer.strip().lower() == "yes"


async def run_write_verify(
    client: RestClient,
    router: RestEventRouter,
    prop: str,
    *,
    timeout_s: float,
) -> None:
    current = await client.get(prop)
    if current is None:
        raise SystemExit(f"{prop} returned no body to write back — cannot verify.")

    if not await confirm_write_verify(prop, current):
        print("Skipped.")
        return

    router.arm(prop)
    print(f"\nPUT {prop} {current!r} (same-value) …")
    await client.put(prop, current)

    print(f"Awaiting WS confirmation (primary check, timeout {timeout_s}s) …")
    event_value = await router.wait_for(prop, timeout=timeout_s)
    if event_value is not None:
        print(f"  PRIMARY OK — WS event arrived: {event_value!r}")
    else:
        print("  PRIMARY: no fresh WS event arrived within the timeout")

    readback = await client.get(prop)
    print(f"Secondary check — GET {prop} readback: {readback!r}")

    if event_value is None and readback != current:
        raise SystemExit(
            "Neither check confirmed the write — this is exactly BMDVerificationError's "
            "job once RestCameraSession exists (Phase 3+)."
        )
    print("\nWrite-verify round trip confirmed by at least one channel.")


async def run(args: argparse.Namespace) -> None:
    require_aiohttp()
    profile = CameraProfile.for_model(args.model_key, args.firmware)
    if not profile.rest.endpoints:
        raise SystemExit(
            f"{args.model_key} {args.firmware} has no rest/ profile — run "
            "tools/rest/probe_endpoints.py against it first."
        )

    base_url = normalise_base_url(args.host, args.scheme)

    async with aiohttp.ClientSession() as session:
        client = RestClient(args.host, scheme=args.scheme, timeout_s=args.timeout, session=session)

        await check_204_returns_none(client)

        readable = pick_readable_endpoint(profile.rest.endpoints, exclude=frozenset({"/system"}))
        if readable:
            await check_200_returns_body(client, readable)
        else:
            print("\n[2/3] Skipped — no confirmed-200 endpoint in this profile besides /system.")

        not_implemented = pick_not_implemented_endpoint(profile.rest.endpoints)
        if not_implemented:
            await check_501_raises_unsupported(client, not_implemented)
        else:
            print("\n[3/3] Skipped — no confirmed-501 endpoint in this profile.")

        print("\nRead-only checks done.")

        if not args.verify_write:
            return

        prop = args.property or pick_write_verify_property(
            profile.rest.endpoints, profile.rest.websocket_properties
        )
        if prop is None:
            raise SystemExit(
                "No endpoint is both put_supported and a confirmed websocket_properties "
                "member in this profile — pass --property explicitly."
            )

        router = RestEventRouter()
        ws_url = websocket_url(base_url)
        await router.connect(session, ws_url, timeout_s=args.timeout)
        await router.subscribe(prop)
        try:
            await run_write_verify(client, router, prop, timeout_s=args.timeout)
        finally:
            await router.disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exercise RestClient (and, opt-in, RestEventRouter's arm()/wait_for()) "
        "against a real camera."
    )
    parser.add_argument(
        "--host",
        required=True,
        help="Camera hostname or IP, e.g. 172.27.97.141. See docs/rest/transport.md "
        "for how to find this over USB.",
    )
    parser.add_argument(
        "--scheme",
        default="http",
        choices=["http", "https"],
        help="URL scheme when --host has none. Default: http.",
    )
    parser.add_argument(
        "--model-key",
        required=True,
        help="Camera model key whose rest/ profile supplies the endpoints to exercise, "
        "e.g. POCKET_6K_PRO.",
    )
    parser.add_argument(
        "--firmware",
        required=True,
        help="Paired with --model-key, e.g. v8.6.",
    )
    parser.add_argument(
        "--verify-write",
        action="store_true",
        help="Also run one full arm()/PUT/wait_for() write-verify round trip on an "
        "auto-picked (or --property-given) endpoint. Typed-yes gated. Idempotent "
        "same-value PUT only.",
    )
    parser.add_argument(
        "--property",
        default=None,
        help="Endpoint to use for --verify-write, overriding auto-pick, e.g. /video/iso.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-request / WS-wait timeout in seconds. Default: 5.0",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(run(parse_args()))
    except BMDRestError as exc:
        raise SystemExit(f"REST error: {exc}") from exc
