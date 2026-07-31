"""
tools/rest/probe_endpoints.py
==============================
Sweeps every endpoint of the Blackmagic Camera 8.6 REST API on a real camera
and records what actually works, so the migration in docs/rest/transport.md
is planned against evidence rather than against the published specs.

WHY THIS EXISTS
───────────────
The specs describe an API; a given camera and firmware implement some subset
of it, sometimes badly. On `POCKET_6K_G2 v8.6` the operator has already found
three defects by hand — `GET /system` returns 204 with no body,
`/system/videoFormat` and `/system/codecFormat` return 501, and listing the
Stills directory returns 500. Those were found by accident. This tool finds
the rest on purpose.

Nothing else in the REST migration starts until this has been run: which
endpoints exist decides which BLE functionality can actually move.

This is the HTTP analogue of design principle 6 ("never copy protocol values
from one profile to another without re-verifying"). Results are recorded per
(model_key, firmware, transport) and are **not** evidence about any other
combination — a `POCKET_6K_PRO v8.6` sweep is a separate run, and so is the
same camera over Wi-Fi instead of USB.

TWO MODES
─────────
`--read-only` (the default) issues GET requests only and opens the WebSocket
to subscribe. It changes nothing on the camera and is safe to re-run.

`--probe-writes` additionally answers "is this PUT implemented?", which no
GET can answer. It does so **idempotently**: read the current value, write
that same value straight back, and record the status code. 501 means not
implemented; 200/204 means implemented. Either answer arrives without
changing a single camera setting. It is gated behind a typed-yes prompt
anyway, matching tools/control/'s convention for anything that writes.

Endpoints that would change state regardless of the value written are never
probed at all — see NEVER_WRITE below.

Honest caveat, recorded in the report itself: a same-value PUT may be a
camera-side no-op, so a 200 proves the *endpoint* exists, not that a
*changing* write applies. That second question belongs to the phase that
actually needs it.

Usage:
    python tools/rest/probe_endpoints.py --host 192.168.1.1 \
        --model-key POCKET_6K_G2 --firmware v8.6
    python tools/rest/probe_endpoints.py --host 192.168.1.1 \
        --model-key POCKET_6K_G2 --firmware v8.6 --probe-writes

Finding the camera's address over USB: the camera presents a USB Ethernet
gadget, so there is no mDNS to resolve (which is why
`\\\\pocket-cinema-camera-6k-g2.local` fails in Explorer). On Windows it
appears as a separate "Remote NDIS" adapter and the camera's IP is that
adapter's default gateway — `ipconfig`, or the camera's own Setup screen.
Run without `--host` to have this tool print the candidate gateways it can
see. If every endpoint reports unreachable, rule out Windows Firewall
classifying the RNDIS adapter as a Public network before concluding the
camera is at fault.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import platform
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover - exercised only without the dependency
    # Deliberately not fatal at import time: every catalog/classification/
    # reporting function in this module is pure and unit-tested without any
    # HTTP dependency. Only the functions that actually talk to the camera
    # need aiohttp, and `require_aiohttp()` reports its absence usefully at
    # the point of use instead of turning `import probe_endpoints` into a
    # hard failure.
    aiohttp = None  # type: ignore[assignment]


def require_aiohttp() -> None:
    """Raise a useful error if the HTTP dependency is missing."""
    if aiohttp is None:
        raise RuntimeError(
            "aiohttp is required to talk to the camera. Install it with:\n"
            "    python -m pip install -r requirements.txt"
        )


logger = logging.getLogger(__name__)

API_BASE = "/control/api/v1"
WS_PATH = "/control/api/v1/event/websocket"
MOUNTS_PATH = "/mounts/"

CAPTURES_DIR = Path(__file__).resolve().parents[1] / "captures" / "rest"

# Audio channel indices to probe. The spec says "channels index from 0" but
# never says how many exist; a 404 for a channel that isn't there is itself a
# result worth recording.
AUDIO_CHANNELS = (0, 1, 2, 3)

# Directory listing under /mounts/ that the operator has already confirmed
# returns 500 on POCKET_6K_G2 v8.6. Probed explicitly so every run either
# reproduces the defect or shows it fixed.
KNOWN_BROKEN_HINT = "Stills"


# ── Result model ────────────────────────────────────────────────────────────


@dataclass
class ProbeResult:
    """One request's outcome. `status` is None when the request never got a
    reply at all (cable unplugged, wrong host, firewall)."""

    method: str
    path: str
    status: int | None = None
    latency_ms: float | None = None
    body: Any = None
    error: str | None = None

    @property
    def classification(self) -> str:
        return classify(self.status, self.error)


def classify(status: int | None, error: str | None = None) -> str:
    """Bucket a response for the summary and for the emitted profile block.

    `not_implemented` (501) is deliberately distinct from `server_error`
    (other 5xx): the first is the camera correctly saying "this device does
    not do that", the second is a firmware defect like the Stills listing.
    Conflating them would hide exactly the class of problem this sweep
    exists to find.
    """
    if error is not None:
        return "unreachable"
    if status is None:
        return "unreachable"
    if status == 501:
        return "not_implemented"
    if status == 404:
        return "not_found"
    if 500 <= status < 600:
        return "server_error"
    if 200 <= status < 300:
        return "ok"
    return "other"


def is_supported(classification: str) -> bool:
    """Whether a caller should be allowed to use this endpoint. Only `ok`
    qualifies — `server_error` is reachable but broken, which is worse than
    absent because it looks like a transient failure at the call site."""
    return classification == "ok"


# ── Write-body builders ─────────────────────────────────────────────────────
#
# A same-value PUT needs the GET response reshaped into what the PUT schema
# accepts: several endpoints report more fields than they accept, or accept a
# choice of mutually-exclusive fields. Each builder returns the body to send,
# or None when this particular reading cannot be safely echoed back.


def echo_body(payload: Any) -> dict | None:
    """Send back exactly what was read. Correct for the endpoints whose GET
    and PUT schemas are the same object (`/video/iso`, `/colorCorrection/*`,
    `/system/format`, …)."""
    if not isinstance(payload, dict) or not payload:
        return None
    return dict(payload)


def pick_first(*keys: str) -> Callable[[Any], dict | None]:
    """Builder keeping only the first present key among `keys`.

    For endpoints whose PUT takes one of several mutually-exclusive fields
    with a documented priority order — e.g. `/lens/iris` reports
    `apertureStop`, `normalised` and `apertureNumber` together but its PUT
    says "if multiple provided, the order of priority is (apertureStop >
    normalised > apertureNumber)". Sending all three would probe a request
    shape no real caller would send.
    """

    def build(payload: Any) -> dict | None:
        if not isinstance(payload, dict):
            return None
        for key in keys:
            if payload.get(key) is not None:
                return {key: payload[key]}
        return None

    return build


def transport_mode_body(payload: Any) -> dict | None:
    """`PUT /transports/0` accepts only `InputPreview` and `Output`, while its
    GET can also report `InputRecord`. Echoing `InputRecord` back would be an
    invalid request — and if the camera is recording, this tool has no
    business touching its transport at all. Skip instead."""
    if not isinstance(payload, dict):
        return None
    mode = payload.get("mode")
    if mode in ("InputPreview", "Output"):
        return {"mode": mode}
    return None


# ── Endpoint catalog ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Endpoint:
    """One endpoint from the published specs.

    `write_body` is None for anything this tool must never PUT — either
    because the spec declares no PUT, or because the write changes state no
    matter what value is sent (see NEVER_WRITE).
    """

    path: str
    write_body: Callable[[Any], dict | None] | None = None
    note: str = ""


# Paths whose writes change camera state regardless of the value sent, so
# there is no idempotent probe for them. Listed by path for documentation
# value even where the catalog already omits the builder.
NEVER_WRITE: tuple[str, ...] = (
    "/media/devices/{deviceName}/doformat",  # erases the card
    "/transports/0/record",  # starts/stops recording
    "/transports/0/play",  # starts playback
    "/transports/0/stop",  # stops playback
    "/timelines/0",  # DELETE clears the timeline
    "/timelines/0/add",  # POST mutates the timeline
    "/lens/focus/doAutoFocus",  # moves the lens
    "/video/whiteBalance/doAuto",  # re-measures white balance
)


def build_catalog() -> list[Endpoint]:
    """Every path from the ten published control specs, plus `/clips/list`,
    which no uploaded spec covers but which the operator has confirmed works.

    Path parameters are left as `{deviceName}` / `{channelIndex}` templates
    here and filled in by `expand_catalog` once the sweep knows real values.
    """
    catalog: list[Endpoint] = [
        # SystemControl
        Endpoint("/system", note="operator saw 204 No Content on POCKET_6K_G2 v8.6"),
        Endpoint("/system/supportedCodecFormats"),
        Endpoint("/system/codecFormat", echo_body, note="operator saw 501 on POCKET_6K_G2 v8.6"),
        Endpoint("/system/videoFormat", echo_body, note="operator saw 501 on POCKET_6K_G2 v8.6"),
        Endpoint("/system/supportedVideoFormats"),
        Endpoint("/system/supportedFormats"),
        Endpoint("/system/format", echo_body, note="the only confirmed-working format surface"),
        # TransportControl
        Endpoint("/transports/0", transport_mode_body),
        Endpoint("/transports/0/stop"),
        Endpoint("/transports/0/play"),
        Endpoint("/transports/0/playback", echo_body),
        Endpoint("/transports/0/record"),
        Endpoint("/transports/0/timecode"),
        Endpoint("/transports/0/timecode/source"),
        # MediaControl
        Endpoint("/media/workingset"),
        Endpoint("/media/active", pick_first("workingsetIndex")),
        Endpoint("/media/devices/doformatSupportedFilesystems"),
        Endpoint("/media/devices/{deviceName}"),
        Endpoint(
            "/media/devices/{deviceName}/doformat",
            note="GET returns a format key; never PUT",
        ),
        # EventControl
        Endpoint("/event/list"),
        # TimelineControl
        Endpoint("/timelines/0"),
        # VideoControl
        Endpoint("/video/iso", echo_body),
        Endpoint("/video/gain", echo_body),
        Endpoint("/video/whiteBalance", echo_body),
        Endpoint("/video/whiteBalanceTint", echo_body),
        Endpoint("/video/ndFilter", echo_body),
        Endpoint("/video/ndFilter/displayMode", echo_body),
        Endpoint("/video/shutter", pick_first("shutterSpeed", "shutterAngle")),
        Endpoint("/video/autoExposure", echo_body),
        # LensControl
        Endpoint("/lens/iris", pick_first("apertureStop", "normalised", "apertureNumber")),
        Endpoint("/lens/zoom", pick_first("focalLength", "normalised")),
        Endpoint("/lens/focus", pick_first("focus")),
        # ColorCorrectionControl
        Endpoint("/colorCorrection/lift", echo_body),
        Endpoint("/colorCorrection/gamma", echo_body),
        Endpoint("/colorCorrection/gain", echo_body),
        Endpoint("/colorCorrection/offset", echo_body),
        Endpoint("/colorCorrection/contrast", echo_body),
        Endpoint("/colorCorrection/color", echo_body),
        Endpoint("/colorCorrection/lumaContribution", echo_body),
        # Not in any uploaded spec — operator-confirmed working
        Endpoint("/clips/list", note="no published spec; shape assumed from an operator sample"),
    ]
    for index in AUDIO_CHANNELS:
        catalog.extend(
            [
                Endpoint(f"/audio/channel/{index}/input", echo_body),
                Endpoint(f"/audio/channel/{index}/input/description"),
                Endpoint(f"/audio/channel/{index}/supportedInputs"),
                Endpoint(f"/audio/channel/{index}/level", pick_first("gain", "normalised")),
                Endpoint(f"/audio/channel/{index}/phantomPower", echo_body),
                Endpoint(f"/audio/channel/{index}/padding", echo_body),
                Endpoint(f"/audio/channel/{index}/lowCutFilter", echo_body),
                Endpoint(f"/audio/channel/{index}/available"),
            ]
        )
    return catalog


def expand_catalog(catalog: list[Endpoint], device_names: list[str]) -> list[Endpoint]:
    """Fill `{deviceName}` templates from live `/media/workingset` data.

    A template with no known device names is dropped rather than requested
    literally — a 404 for the path `/media/devices/{deviceName}` would be a
    fact about this tool, not about the camera.
    """
    expanded: list[Endpoint] = []
    for endpoint in catalog:
        if "{deviceName}" not in endpoint.path:
            expanded.append(endpoint)
            continue
        for name in device_names:
            expanded.append(
                Endpoint(
                    path=endpoint.path.replace("{deviceName}", name),
                    write_body=endpoint.write_body,
                    note=endpoint.note,
                )
            )
    return expanded


def device_names_from_workingset(payload: Any) -> list[str]:
    """Device names from a `/media/workingset` body, in working-set order."""
    if not isinstance(payload, dict):
        return []
    entries = payload.get("workingset")
    if not isinstance(entries, list):
        return []
    names = [
        entry["deviceName"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("deviceName"), str)
    ]
    return list(dict.fromkeys(names))


def subscribable_properties(event_list_payload: Any) -> list[str]:
    """Property names from a `/event/list` body.

    The camera is the authority on what can be subscribed to; the spec's
    `x-propertyName` enum is only a hypothesis. Callers merge the two so a
    property the camera forgot to advertise still gets tried.
    """
    if isinstance(event_list_payload, dict):
        events = event_list_payload.get("events")
        if isinstance(events, list):
            return [event for event in events if isinstance(event, str)]
    if isinstance(event_list_payload, list):
        return [event for event in event_list_payload if isinstance(event, str)]
    return []


# Every `x-propertyName` in Notification.yaml, tried in addition to whatever
# /event/list reports so the two can be compared.
SPEC_PROPERTIES: tuple[str, ...] = (
    "/media/active",
    "/system",
    "/system/codecFormat",
    "/system/videoFormat",
    "/system/format",
    "/system/supportedFormats",
    "/timelines/0",
    "/transports/0",
    "/transports/0/stop",
    "/transports/0/play",
    "/transports/0/playback",
    "/transports/0/record",
    "/transports/0/timecode",
    "/transports/0/timecode/source",
)


# ── HTTP sweep ──────────────────────────────────────────────────────────────


async def request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    json_body: dict | None = None,
) -> ProbeResult:
    """One request, never raising. A transport failure is a result too."""
    started = time.monotonic()
    try:
        async with session.request(method, url, json=json_body) as response:
            body: Any = None
            if response.status != 204:
                text = await response.text()
                if text:
                    try:
                        body = json.loads(text)
                    except json.JSONDecodeError:
                        body = text[:500]
            latency_ms = (time.monotonic() - started) * 1000
            return ProbeResult(method, url, response.status, latency_ms, body)
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        latency_ms = (time.monotonic() - started) * 1000
        return ProbeResult(method, url, None, latency_ms, error=f"{type(exc).__name__}: {exc}")


async def sweep_reads(
    session: aiohttp.ClientSession, base_url: str, catalog: list[Endpoint]
) -> list[ProbeResult]:
    """GET every catalog path in order, one at a time.

    Deliberately sequential: a camera serving this over a USB gadget is not a
    load-test target, and a stampede of parallel requests would make any
    failure impossible to attribute to a specific endpoint.
    """
    results: list[ProbeResult] = []
    for endpoint in catalog:
        result = await request(session, "GET", f"{base_url}{API_BASE}{endpoint.path}")
        result.path = endpoint.path
        results.append(result)
        logger.info(
            "GET %-52s -> %s (%s)",
            endpoint.path,
            result.status if result.status is not None else "no reply",
            result.classification,
        )
    return results


async def sweep_writes(
    session: aiohttp.ClientSession,
    base_url: str,
    catalog: list[Endpoint],
    read_results: dict[str, ProbeResult],
) -> list[ProbeResult]:
    """Idempotent PUT probe — see the module docstring.

    Only endpoints that (a) declare a write body builder, (b) read back
    successfully, and (c) produce a non-None body from that builder are
    probed. Everything else is skipped rather than guessed at, because a
    fabricated request body would test this tool's imagination rather than
    the camera.
    """
    results: list[ProbeResult] = []
    for endpoint in catalog:
        if endpoint.write_body is None or endpoint.path in NEVER_WRITE:
            continue
        read = read_results.get(endpoint.path)
        if read is None or not is_supported(read.classification):
            continue
        body = endpoint.write_body(read.body)
        if body is None:
            logger.info("PUT  %-52s -> skipped (no echoable value read back)", endpoint.path)
            continue
        result = await request(
            session, "PUT", f"{base_url}{API_BASE}{endpoint.path}", json_body=body
        )
        result.path = endpoint.path
        results.append(result)
        logger.info(
            "PUT  %-52s -> %s (%s) body=%s",
            endpoint.path,
            result.status if result.status is not None else "no reply",
            result.classification,
            json.dumps(body)[:80],
        )
    return results


# ── Mounts walk ─────────────────────────────────────────────────────────────

_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)


def extract_links(body: Any) -> list[str]:
    """Directory entries from a Web Media Manager listing.

    The listing is HTML, so this is a deliberately shallow regex over href
    attributes rather than a parser — enough to walk one or two levels and
    find the volume names, without taking on an HTML dependency for a
    diagnostic tool. Parent links and absolute URLs are dropped.
    """
    if not isinstance(body, str):
        return []
    links = []
    for href in _HREF_RE.findall(body):
        if href in ("..", "../", "/", "") or href.startswith(("http://", "https://", "#", "?")):
            continue
        links.append(href.lstrip("/"))
    return list(dict.fromkeys(links))


async def walk_mounts(
    session: aiohttp.ClientSession, base_url: str, *, max_depth: int = 2
) -> list[ProbeResult]:
    """Walk `/mounts/` breadth-first to `max_depth`, recording each level.

    The Stills directory is known to return 500 on `POCKET_6K_G2 v8.6`. This
    walk establishes whether that defect is specific to Stills or affects
    other directories too — which decides whether Phase 6's photo
    confirmation can list anything at all or has to probe filenames.
    """
    results: list[ProbeResult] = []
    frontier = [MOUNTS_PATH]
    seen: set[str] = set()

    for _depth in range(max_depth + 1):
        next_frontier: list[str] = []
        for path in frontier:
            if path in seen:
                continue
            seen.add(path)
            result = await request(session, "GET", f"{base_url}{path}")
            result.path = path
            results.append(result)
            logger.info(
                "GET %-52s -> %s (%s)",
                path,
                result.status if result.status is not None else "no reply",
                result.classification,
            )
            if not is_supported(result.classification):
                continue
            for link in extract_links(result.body):
                child = f"{path.rstrip('/')}/{link}"
                if not child.endswith("/"):
                    child += "/"
                next_frontier.append(child)
        frontier = next_frontier
        if not frontier:
            break

    return results


# ── WebSocket probe ─────────────────────────────────────────────────────────


def ws_response_ok(message: Any) -> bool:
    """Whether a WS response reports success.

    Handles both shapes seen in practice: the spec nests `success` under
    `data`, while the camera has been observed replying with it at the top
    level (`{"type": "response", "success": true}`).
    """
    if not isinstance(message, dict):
        return False
    if isinstance(message.get("success"), bool):
        return message["success"]
    data = message.get("data")
    if isinstance(data, dict) and isinstance(data.get("success"), bool):
        return data["success"]
    return False


async def probe_websocket(
    session: aiohttp.ClientSession,
    base_url: str,
    properties: list[str],
    *,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    """Subscribe to each property individually and record which succeed.

    One property per request, not one batch: a batch that fails tells you
    nothing about which member caused it. Same reasoning as one capture
    window per setting in the BLE sniffers.
    """
    outcome: dict[str, Any] = {"connected": False, "error": None, "subscriptions": {}}
    ws_url = f"{base_url.replace('http://', 'ws://', 1)}{WS_PATH}"
    try:
        async with session.ws_connect(ws_url, timeout=timeout_s) as ws:
            outcome["connected"] = True
            for index, prop in enumerate(properties):
                await ws.send_json(
                    {
                        "type": "request",
                        "id": index,
                        "data": {"action": "subscribe", "properties": [prop]},
                    }
                )
                try:
                    message = await asyncio.wait_for(ws.receive_json(), timeout=timeout_s)
                    ok = ws_response_ok(message)
                except (TimeoutError, ValueError, TypeError):
                    message, ok = None, False
                outcome["subscriptions"][prop] = {"success": ok, "response": message}
                logger.info("WS  subscribe %-46s -> %s", prop, "ok" if ok else "FAILED")
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("WebSocket probe failed: %s", outcome["error"])
    return outcome


# ── Reporting ───────────────────────────────────────────────────────────────


@dataclass
class Report:
    model_key: str
    firmware: str
    transport: str
    host: str
    probed_at: str
    probe_writes: bool
    reads: list[ProbeResult] = field(default_factory=list)
    writes: list[ProbeResult] = field(default_factory=list)
    mounts: list[ProbeResult] = field(default_factory=list)
    websocket: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "model_key": self.model_key,
            "firmware": self.firmware,
            "transport": self.transport,
            "host": self.host,
            "probed_at": self.probed_at,
            "probe_writes": self.probe_writes,
            "_caveat": (
                "A same-value PUT proves the endpoint exists, not that a changing write "
                "applies. Results are evidence about this (model, firmware, transport) "
                "only."
            ),
            "reads": [_result_dict(result) for result in self.reads],
            "writes": [_result_dict(result) for result in self.writes],
            "mounts": [_result_dict(result) for result in self.mounts],
            "websocket": self.websocket,
        }


def _result_dict(result: ProbeResult) -> dict:
    return {
        "method": result.method,
        "path": result.path,
        "status": result.status,
        "classification": result.classification,
        "latency_ms": round(result.latency_ms, 1) if result.latency_ms is not None else None,
        "body": result.body,
        "error": result.error,
    }


def render_summary(report: Report) -> str:
    """Human-readable summary grouped by classification, worst first.

    Ordering is deliberate: `server_error` is what this sweep exists to
    surface, so it goes at the top rather than being buried under forty
    working endpoints.
    """
    lines = [
        "",
        "=== Endpoint sweep summary ===",
        f"  camera:    {report.model_key} {report.firmware}",
        f"  transport: {report.transport}  host: {report.host}",
        f"  probed at: {report.probed_at}",
        "",
    ]
    buckets: dict[str, list[ProbeResult]] = {}
    for result in [*report.reads, *report.writes, *report.mounts]:
        buckets.setdefault(result.classification, []).append(result)

    order = ["server_error", "unreachable", "other", "not_implemented", "not_found", "ok"]
    labels = {
        "server_error": "5xx FIRMWARE DEFECT (reachable but broken)",
        "unreachable": "NO REPLY (check the cable, host, and firewall)",
        "other": "UNEXPECTED STATUS",
        "not_implemented": "501 NOT IMPLEMENTED on this device",
        "not_found": "404 NOT FOUND",
        "ok": "WORKING",
    }
    for key in order:
        entries = buckets.get(key)
        if not entries:
            continue
        lines.append(f"{labels[key]}  ({len(entries)})")
        for result in entries:
            status = result.status if result.status is not None else "---"
            lines.append(f"    {result.method:4s} {result.path:<52s} {status}")
        lines.append("")

    subscriptions = report.websocket.get("subscriptions", {})
    if subscriptions:
        ok = [prop for prop, value in subscriptions.items() if value.get("success")]
        failed = [prop for prop, value in subscriptions.items() if not value.get("success")]
        lines.append(f"WEBSOCKET  subscribed {len(ok)}/{len(subscriptions)}")
        for prop in failed:
            lines.append(f"    FAILED  {prop}")
        lines.append("")
    elif report.websocket.get("error"):
        lines.append(f"WEBSOCKET  could not connect: {report.websocket['error']}")
        lines.append("")

    return "\n".join(lines)


def build_rest_profile_block(report: Report) -> dict:
    """The `payloads/models/<MODEL_KEY>/rest/<FIRMWARE>.json` content this
    sweep justifies — ready to paste, the same way
    tools/control/discover_command.py emits a `commands` block.

    `format_names` is left empty on purpose. Deriving the REST codec strings
    needs `/system/supportedFormats`, and inventing them from a naming rule
    would be exactly the kind of unverified protocol value design principle 1
    exists to keep out of this repo. Fill it from the sweep's own
    `supportedFormats` body once that endpoint is confirmed working.
    """
    endpoints: dict[str, Any] = {}
    for result in [*report.reads, *report.mounts]:
        entry: dict[str, Any] = {
            "status": result.status,
            "supported": is_supported(result.classification),
        }
        if result.error is not None:
            entry["notes"] = result.error
        endpoints[result.path] = entry
    for result in report.writes:
        entry = endpoints.setdefault(result.path, {})
        entry["put_status"] = result.status
        entry["put_supported"] = is_supported(result.classification)

    subscriptions = report.websocket.get("subscriptions", {})
    return {
        "_meta": {
            "model_key": report.model_key,
            "firmware": report.firmware,
            "status": "UNVERIFIED",
        },
        "transport": report.transport,
        "endpoints": endpoints,
        "websocket_properties": sorted(
            prop for prop, value in subscriptions.items() if value.get("success")
        ),
        "format_names": {},
        "provenance": {
            "status": "CANDIDATE",
            "method": (
                f"tools/rest/probe_endpoints.py sweep over {report.transport}"
                f"{', including idempotent write probes' if report.probe_writes else ''}"
            ),
            "verified_on": report.probed_at[:10],
            "notes": (
                "Swept on this camera and firmware only — not evidence about any other "
                "model, firmware, or transport. A same-value PUT proves the endpoint "
                "exists, not that a changing write applies."
            ),
        },
    }


def save_report(report: Report, *, captures_dir: Path = CAPTURES_DIR) -> Path:
    """Write the raw report next to the BLE captures, gitignored."""
    directory = captures_dir / f"{report.model_key}_{report.firmware}"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = report.probed_at.replace(":", "-")
    path = directory / f"rest_probe_{stamp}.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report.to_dict(), handle, indent=2)
    return path


# ── Host discovery help ─────────────────────────────────────────────────────


def parse_windows_gateways(ipconfig_output: str) -> list[str]:
    """Default-gateway addresses from `ipconfig` output.

    Best-effort assistance only — the camera's USB gadget appears as one more
    adapter and its gateway is the camera. This does not try to identify
    *which* adapter is the camera; it prints candidates for the operator to
    choose from, because guessing wrong here means sweeping some other device
    on the network and recording the results as if they were the camera's.
    """
    gateways = []
    for line in ipconfig_output.splitlines():
        if "Default Gateway" not in line:
            continue
        _, _, value = line.partition(":")
        candidate = value.strip()
        if candidate and re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", candidate):
            gateways.append(candidate)
    return list(dict.fromkeys(gateways))


def suggest_hosts() -> list[str]:
    """Candidate camera addresses, or an empty list when none can be found."""
    if platform.system() != "Windows":
        return []
    try:
        completed = subprocess.run(
            ["ipconfig"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_windows_gateways(completed.stdout)


async def confirm_write_probe(host: str) -> bool:
    """Typed-yes gate before any PUT, matching tools/control/'s convention."""
    print(f"\nAbout to probe writes against {host}.")
    print(
        "\nEvery PUT sends back the value just read, so no setting should change.\n"
        "Destructive endpoints (format, record, play/stop, timeline edits, autofocus,\n"
        "auto white balance) are never probed at all. Even so: keep the camera in view,\n"
        "note its current settings, and stop if anything moves."
    )
    loop = asyncio.get_running_loop()
    answer = await loop.run_in_executor(None, input, "Type 'yes' to proceed: ")
    return answer.strip().lower() == "yes"


# ── Entry point ─────────────────────────────────────────────────────────────


async def run(args: argparse.Namespace) -> int:
    require_aiohttp()
    if not args.host:
        candidates = suggest_hosts()
        print("--host is required. The camera's address over USB is the USB Ethernet")
        print("adapter's default gateway (ipconfig, or the camera's Setup screen).")
        if candidates:
            print("\nCandidate gateways on this machine:")
            for candidate in candidates:
                print(f"    {candidate}")
            print("\nPick the one belonging to the camera's adapter — not your LAN router.")
        return 2

    base_url = f"http://{args.host}"
    report = Report(
        model_key=args.model_key,
        firmware=args.firmware,
        transport=args.transport,
        host=args.host,
        probed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ"),
        probe_writes=args.probe_writes,
    )

    timeout = aiohttp.ClientTimeout(total=args.timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        workingset = await request(session, "GET", f"{base_url}{API_BASE}/media/workingset")
        device_names = device_names_from_workingset(workingset.body)
        if device_names:
            logger.info("Media devices discovered: %s", ", ".join(device_names))
        else:
            logger.warning(
                "No media device names read from /media/workingset — "
                "/media/devices/{deviceName} paths will be skipped"
            )

        catalog = expand_catalog(build_catalog(), device_names)
        report.reads = await sweep_reads(session, base_url, catalog)
        read_by_path = {result.path: result for result in report.reads}

        report.mounts = await walk_mounts(session, base_url, max_depth=args.mounts_depth)
        if not any(KNOWN_BROKEN_HINT.lower() in result.path.lower() for result in report.mounts):
            logger.warning(
                "No %s directory reached in the mounts walk — the known 500 defect was "
                "neither reproduced nor disproved. Try --mounts-depth 3.",
                KNOWN_BROKEN_HINT,
            )

        event_list = read_by_path.get("/event/list")
        camera_properties = subscribable_properties(event_list.body) if event_list else []
        properties = list(dict.fromkeys([*camera_properties, *SPEC_PROPERTIES]))
        report.websocket = await probe_websocket(session, base_url, properties)
        report.websocket["event_list"] = camera_properties

        if args.probe_writes:
            if await confirm_write_probe(args.host):
                report.writes = await sweep_writes(session, base_url, catalog, read_by_path)
            else:
                print("Write probe declined — read-only results kept.")

    print(render_summary(report))

    saved = save_report(report)
    print(f"Raw report saved to: {saved}")

    block_path = saved.with_name(saved.stem + "_profile_block.json")
    with open(block_path, "w", encoding="utf-8") as handle:
        json.dump(build_rest_profile_block(report), handle, indent=2)
    print(f"Profile block saved to: {block_path}")
    print(
        "\nReview the summary against the gate table in docs/rest/transport.md before "
        "\nbuilding anything on these results."
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sweep every Blackmagic Camera 8.6 REST endpoint and record what works. "
            "Read-only by default; --probe-writes adds idempotent same-value PUTs."
        )
    )
    parser.add_argument("--host", help="Camera IP address (the USB adapter's default gateway).")
    parser.add_argument(
        "--model-key",
        required=True,
        help="Camera model key the results are recorded against, e.g. POCKET_6K_G2.",
    )
    parser.add_argument(
        "--firmware",
        required=True,
        help="Camera firmware the results are recorded against, e.g. v8.6.",
    )
    parser.add_argument(
        "--transport",
        default="usb",
        choices=["usb", "lan"],
        help=(
            "How the camera is connected. Recorded in the report because an endpoint "
            "verified over one transport is not evidence about the other. Default: usb"
        ),
    )
    parser.add_argument(
        "--probe-writes",
        action="store_true",
        help=(
            "Also probe whether each PUT is implemented, by writing back the value just "
            "read. Typed-yes gated. Destructive endpoints are never probed."
        ),
    )
    parser.add_argument(
        "--mounts-depth",
        type=int,
        default=2,
        help="How deep to walk the /mounts/ tree. Default: 2",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Per-request timeout in seconds. Default: 10.0",
    )
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(run(parse_args())))
