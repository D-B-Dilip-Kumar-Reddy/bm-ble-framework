"""
tools/rest/watch_events.py
============================
Stream `propertyValueChanged` WebSocket events from a Blackmagic Camera 8.6
REST API in real time — the REST analogue of examples/monitor_incoming.py.

Run this script, then change settings on the camera body (or over REST from
another session). Each event is logged with its property path and value, so
the output can be eyeballed against the profile's `websocket_properties`
list and against docs/rest/transport.md's write-up of what actually
subscribes on real hardware.

Unlike tools/rest/probe_endpoints.py (deliberately standalone, no
bmd_camera imports at all — see its module docstring), this tool exercises
the Phase 2 library surface directly: `RestEventRouter` for buffering and
subscription/reconnect bookkeeping, `RestClient` only to fetch a camera
profile's confirmed endpoint list when `--properties` is omitted.

Usage:
    python tools/rest/watch_events.py --host pocket-cinema-camera-6k-g2.local \
        --model-key POCKET_6K_G2 --firmware v8.6

    python tools/rest/watch_events.py --host pocket-cinema-camera-6k-g2.local \
        --properties /transports/0/record,/system/format

Press Ctrl+C to stop and disconnect cleanly.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from datetime import UTC, datetime

try:
    import aiohttp
except ImportError:  # pragma: no cover - exercised only without the dependency
    aiohttp = None  # type: ignore[assignment]

from bmd_camera.camera_profile import CameraProfile
from bmd_camera.rest.constants import WS_PATH
from bmd_camera.rest.events import RestEventRouter, require_aiohttp

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


def _log_event(prop: str, value: object) -> None:
    timestamp = datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3]
    logger.info("%s  %-32s  %s", timestamp, prop, value)


def resolve_properties(args: argparse.Namespace) -> list[str]:
    """Explicit `--properties` wins. Otherwise, if a profile has already
    been swept, subscribe to every property it confirmed subscribes on real
    hardware (`websocket_properties`, populated by
    tools/rest/probe_endpoints.py) — this is what design principle 6's REST
    sibling rule means in practice: don't guess a property list, use the
    one confirmed for this exact camera and firmware."""
    if args.properties:
        return [p.strip() for p in args.properties.split(",") if p.strip()]
    if args.model_key and args.firmware:
        profile = CameraProfile.for_model(args.model_key, args.firmware)
        if profile.rest.websocket_properties:
            return list(profile.rest.websocket_properties)
        logger.warning(
            "[%s @ %s] No rest/ profile (or an empty websocket_properties list) — "
            "pass --properties explicitly.",
            args.model_key,
            args.firmware,
        )
    raise SystemExit(
        "Nothing to subscribe to — pass --properties, or --model-key/--firmware for a "
        "camera with a populated rest/<firmware>.json (see payloads/models/*/rest/)."
    )


async def run(args: argparse.Namespace) -> None:
    require_aiohttp()
    properties = resolve_properties(args)
    base_url = normalise_base_url(args.host, args.scheme)
    ws_url = websocket_url(base_url)

    router = RestEventRouter(on_event=_log_event)
    stop = asyncio.Event()

    async with aiohttp.ClientSession() as session:
        logger.info("Connecting to %s …", ws_url)
        await router.connect(session, ws_url, timeout_s=args.timeout)
        for prop in properties:
            await router.subscribe(prop)
        plural = "y" if len(properties) == 1 else "ies"
        logger.info("Subscribed to %d propert%s — watching …", len(properties), plural)

        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError):  # e.g. some Windows event loops
            loop.add_signal_handler(signal.SIGINT, stop.set)

        try:
            if args.duration:
                await asyncio.wait_for(stop.wait(), timeout=args.duration)
            else:
                await stop.wait()
        except TimeoutError:
            logger.info("Duration elapsed — stopping.")
        except asyncio.CancelledError:
            pass
        finally:
            await router.disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream propertyValueChanged WebSocket events from a Blackmagic Camera "
        "8.6 REST API in real time."
    )
    parser.add_argument(
        "--host",
        required=True,
        help="Camera hostname or IP, e.g. pocket-cinema-camera-6k-g2.local. See "
        "docs/rest/transport.md for how to find this over USB.",
    )
    parser.add_argument(
        "--scheme",
        default="http",
        choices=["http", "https"],
        help="URL scheme when --host has none. Default: http.",
    )
    parser.add_argument(
        "--properties",
        help="Comma-separated property paths to subscribe to, e.g. "
        "/transports/0/record,/system/format. Overrides --model-key/--firmware.",
    )
    parser.add_argument(
        "--model-key",
        help="Subscribe to every property this camera/firmware's rest/ profile confirmed "
        "subscribes (websocket_properties). Ignored if --properties is given.",
    )
    parser.add_argument(
        "--firmware",
        help="Paired with --model-key.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="WebSocket connect timeout in seconds. Default: 5.0",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Auto-stop after this many seconds. Default: run until Ctrl+C.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(run(parse_args()))
