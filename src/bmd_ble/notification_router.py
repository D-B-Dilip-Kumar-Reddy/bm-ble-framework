"""
bmd_ble/notification_router.py
================================
Buffers INCOMING_CONTROL notifications and routes them by (category,
parameter), so `session.py` can await a fresh echo after sending a command.

Feature-agnostic — no knowledge of what any category/parameter means. That
lives in `protocol/categories/<category>.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .protocol.codec import CommandHeader, decode_packet


class NotificationRouter:
    """Buffers decoded INCOMING_CONTROL notifications by (category, parameter).

    Buffering starts as soon as `handle_incoming` is subscribed as the
    INCOMING_CONTROL callback — independent of any particular write. Per
    CLAUDE.md's verification strategy, this must happen *before* a command
    is written, or the echo could arrive and be missed before anything is
    listening for it.

    Some commands (e.g. recording start/stop) share a (category, parameter)
    key across opposite states, and the camera has been observed
    retransmitting an echo more than once for a single command. A plain
    "clear then wait" design is vulnerable to two distinct races here:

    1. A delivery that arrived (and was buffered) *before* `arm()` is called
       could otherwise be mistaken for a fresh one. Guarded against with a
       per-key monotonic sequence number: `arm()` records the sequence
       number as of arm time, and `wait_for` only accepts a delivery whose
       sequence number is strictly greater.
    2. A *duplicate retransmit* of the previous command's echo can arrive
       chronologically after the next command's `arm()` but before that
       next command's real echo. Its sequence number alone looks "fresh",
       but its bytes are identical to the echo already returned for the
       prior command. Since commands sharing a key always change the
       payload (recording start/stop toggle between two distinct values),
       `wait_for` also rejects a delivery whose payload matches the last
       payload it itself returned for that key, and keeps waiting for a
       genuinely different one.

    Both checks together mean `wait_for` only ever returns a delivery that
    is both new-in-sequence and different in content from what the previous
    call for that key consumed. See docs/session_and_verification.md.
    """

    def __init__(self) -> None:
        self._latest: dict[tuple[int, int], tuple[int, CommandHeader, bytes]] = {}
        self._seq: dict[tuple[int, int], int] = {}
        self._events: dict[tuple[int, int], asyncio.Event] = {}
        self._last_consumed_payload: dict[tuple[int, int], bytes] = {}
        self._armed_baseline: dict[tuple[int, int], int] = {}

    def handle_incoming(self, _characteristic: Any, data: bytearray) -> None:
        """Bleak-style callback(characteristic, data). Never raises.

        Notifications that don't decode as a BMD command packet (e.g. a
        malformed or unrelated payload) are silently discarded — this is a
        continuous buffer for many possible categories, not a single
        request/response pair.
        """
        try:
            header, payload = decode_packet(bytes(data))
        except ValueError:
            return

        key = (header.category, header.parameter)
        seq = self._seq.get(key, 0) + 1
        self._seq[key] = seq
        self._latest[key] = (seq, header, payload)
        self._events.setdefault(key, asyncio.Event()).set()

    def arm(self, category: int, parameter: int) -> None:
        """Mark the current sequence number as the baseline for (category, parameter).

        Call this immediately before writing a command, so a delivery
        already buffered from an earlier action can't satisfy the upcoming
        `wait_for` — see the class docstring for the full staleness story.
        """
        key = (category, parameter)
        self._events.setdefault(key, asyncio.Event())
        self._armed_baseline[key] = self._seq.get(key, 0)

    async def wait_for(
        self, category: int, parameter: int, timeout: float
    ) -> tuple[CommandHeader, bytes] | None:
        """Await a fresh (post-`arm()`, content-distinct) match. `None` on timeout."""
        key = (category, parameter)
        baseline = self._armed_baseline.get(key, 0)
        last_consumed = self._last_consumed_payload.get(key)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None

            event = self._events.setdefault(key, asyncio.Event())
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                return None

            event.clear()
            seq, header, payload = self._latest[key]
            if seq > baseline and payload != last_consumed:
                self._last_consumed_payload[key] = payload
                return header, payload
            # Stale delivery, or a retransmitted duplicate of the previously
            # consumed echo for this key — keep waiting.
