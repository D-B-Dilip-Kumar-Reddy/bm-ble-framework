"""
bmd_camera/rest/events.py
===========================
RestEventRouter — buffers WebSocket `propertyValueChanged` events and routes
them by property path. Mirrors ble/notification_router.py's `arm()` /
`wait_for()` freshness contract exactly — same staleness/duplicate-delivery
discipline (see docs/ble/session_and_verification.md), keyed by property
string instead of (category, parameter).

MESSAGE SHAPES
────────────────
Exactly the `Notification.yaml` AsyncAPI contract (the uploaded spec, not a
guess):

    request:  {"type": "request", "id": <int>, "data": {"action": "subscribe", "properties": [...]}}
    response: {"type": "response", "id": <int>, "data": {..., "success": bool}}
    event:    {"type": "event", "data": {"action": "propertyValueChanged",
                                          "property": "<path>", "value": {...}}}

`tools/rest/probe_endpoints.py`'s `is_response_to` already found that this
camera interleaves unsolicited messages (an undocumented `websocketOpened`
event observed on real hardware) with responses — reading "the next message"
silently misattributes results. `handle_event` guards the same way: anything
that isn't a well-formed `propertyValueChanged` event is silently ignored
rather than routed, so a `response` message or an unrecognised event type can
never be mistaken for a property update.

Caveat carried honestly forward: the exact `propertyValueChanged` event body
is spec-derived (`Notification.yaml`), not yet cross-checked against a raw
captured event from real hardware — no sweep run to date has logged a full
event body. Confirm this shape against a real WS session before relying on
it for a verification-critical write.

RECONNECTION
──────────────
Subscriptions are per-connection — a reconnect must resubscribe every
property, the same lesson docs/ble/winrt_ble_connection_hardening.md records
for BLE CCCD subscriptions surviving an OS-level reconnect. `connect()`
tracks a connection generation so a reader task from a superseded connection
can never deliver into the router after a newer `connect()` call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover - exercised only without the dependency
    aiohttp = None  # type: ignore[assignment]

from .constants import DEFAULT_WS_TIMEOUT_S
from .exceptions import BMDConnectionError

logger = logging.getLogger(__name__)


def require_aiohttp() -> None:
    """Raise a useful error if the HTTP dependency is missing."""
    if aiohttp is None:
        raise RuntimeError(
            "aiohttp is required to talk to the camera over REST. Install it with:\n"
            "    python -m pip install -r requirements.txt"
        )


def is_property_event(message: Any) -> bool:
    """Whether `message` is a well-formed `propertyValueChanged` event."""
    if not isinstance(message, dict) or message.get("type") != "event":
        return False
    data = message.get("data")
    if not isinstance(data, dict):
        return False
    return data.get("action") == "propertyValueChanged" and isinstance(data.get("property"), str)


class RestEventRouter:
    """Buffers `propertyValueChanged` events by property path.

    Same freshness discipline as NotificationRouter: `arm(prop)` snapshots
    the current sequence number and value for a property immediately before
    a write, and `wait_for(prop, timeout)` only returns a delivery that is
    both new-in-sequence and different in value from what `arm()` had
    already seen. See NotificationRouter's class docstring for the full
    stale/duplicate-delivery story this mirrors — the two checks exist for
    the identical reason there.
    """

    def __init__(self, *, on_event: Any | None = None) -> None:
        """`on_event`, if given, is called `on_event(prop, value)` for every
        routed `propertyValueChanged` event — a hook for a caller that wants
        to observe the whole stream (e.g. `tools/rest/watch_events.py`)
        rather than only ever `wait_for()` one property at a time. Mirrors
        `camera_controller.py`'s "Custom callbacks" pattern for BLE
        (docs/ble/event_subscription_and_logging.md)."""
        self._latest: dict[str, tuple[int, Any]] = {}
        self._seq: dict[str, int] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._armed_baseline: dict[str, int] = {}
        self._armed_snapshot_value: dict[str, Any] = {}
        self._ws: Any | None = None
        self._reader_task: asyncio.Task | None = None
        self._generation = 0
        self._subscribed: set[str] = set()
        self._on_event = on_event

    # ── Buffering ────────────────────────────────────────────────────────

    def handle_event(self, message: Any) -> None:
        """Route one decoded WS message. Never raises — a malformed or
        unrelated message (a response, an unrecognised event type) is
        silently discarded; this is a continuous buffer for many properties,
        not a single request/response pair."""
        if not is_property_event(message):
            return
        data = message["data"]
        prop = data["property"]
        value = data.get("value")
        if self._on_event is not None:
            self._on_event(prop, value)
        seq = self._seq.get(prop, 0) + 1
        self._seq[prop] = seq
        self._latest[prop] = (seq, value)
        self._events.setdefault(prop, asyncio.Event()).set()

    def arm(self, prop: str) -> None:
        """Snapshot the current sequence number and value for `prop`. Call
        immediately before issuing the PUT — the same "buffer before you
        write" rule design principle 3 states for BLE."""
        self._events.setdefault(prop, asyncio.Event())
        self._armed_baseline[prop] = self._seq.get(prop, 0)
        latest = self._latest.get(prop)
        self._armed_snapshot_value[prop] = latest[1] if latest is not None else None

    async def wait_for(self, prop: str, timeout: float) -> Any:
        """Await a fresh (post-`arm()`, value-distinct) delivery for `prop`.

        Returns the new value, or `None` on timeout. `None` is also a
        theoretically valid property value, so a caller that must
        distinguish "timed out" from "value became None" should check
        elapsed time itself — no property in the current spec is
        nullable, so this hasn't mattered in practice yet.
        """
        baseline = self._armed_baseline.get(prop, 0)
        snapshot = self._armed_snapshot_value.get(prop)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None

            event = self._events.setdefault(prop, asyncio.Event())
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                return None

            event.clear()
            seq, value = self._latest[prop]
            if seq > baseline and value != snapshot:
                return value
            # Stale delivery, or a duplicate of what arm() already saw —
            # keep waiting.

    # ── Connection lifecycle ─────────────────────────────────────────────

    async def connect(
        self,
        session: Any,
        ws_url: str,
        *,
        timeout_s: float = DEFAULT_WS_TIMEOUT_S,
    ) -> None:
        """Open the WebSocket and start the background reader.

        Closes any existing connection first, then bumps the connection
        generation before opening the new one, so `_read_loop` for a
        connection that is still winding down can never deliver into this
        router after a newer `connect()` call — the same connection-
        generation guard docs/ble/winrt_ble_connection_hardening.md uses for
        BLE reconnects.

        Every property this router was subscribed to before the (re)connect
        is resubscribed automatically, since the camera only knows about
        subscriptions made on the connection that requested them.
        """
        require_aiohttp()
        await self.disconnect()
        self._generation += 1
        generation = self._generation
        try:
            self._ws = await session.ws_connect(ws_url, timeout=timeout_s)
        except aiohttp.ClientError as exc:
            raise BMDConnectionError(f"WebSocket connect to {ws_url} failed: {exc}") from exc
        self._reader_task = asyncio.ensure_future(self._read_loop(generation))

        to_resubscribe, self._subscribed = self._subscribed, set()
        for prop in to_resubscribe:
            await self.subscribe(prop)

    async def disconnect(self) -> None:
        """Stop the reader and close the socket, if either is open."""
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def subscribe(self, prop: str, *, request_id: int = 0) -> None:
        """Subscribe to `prop`'s `propertyValueChanged` events.

        Tracked in `_subscribed` so a future reconnect resubscribes it
        automatically without the caller having to remember every property
        it ever armed.
        """
        if self._ws is None:
            raise BMDConnectionError("RestEventRouter is not connected — call connect() first")
        await self._ws.send_json(
            {
                "type": "request",
                "id": request_id,
                "data": {"action": "subscribe", "properties": [prop]},
            }
        )
        self._subscribed.add(prop)

    async def _read_loop(self, generation: int) -> None:
        """Background task: decode every incoming WS text frame and route
        it, until cancelled or the connection ends.

        Every message this generation's connection was superseded by a
        newer `connect()` call before delivery is silently dropped rather
        than routed — `generation != self._generation` is the guard.
        """
        ws = self._ws
        assert ws is not None
        try:
            async for msg in ws:
                if generation != self._generation:
                    return
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        message = msg.json()
                    except ValueError:
                        continue
                    self.handle_event(message)
        except asyncio.CancelledError:
            raise
        except aiohttp.ClientError:
            logger.warning("WebSocket reader for generation %d ended unexpectedly", generation)
