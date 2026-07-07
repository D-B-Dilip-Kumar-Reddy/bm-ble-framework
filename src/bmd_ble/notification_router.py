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
    """

    def __init__(self) -> None:
        self._latest: dict[tuple[int, int], tuple[CommandHeader, bytes]] = {}
        self._events: dict[tuple[int, int], asyncio.Event] = {}

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
        self._latest[key] = (header, payload)
        self._events.setdefault(key, asyncio.Event()).set()

    def arm(self, category: int, parameter: int) -> None:
        """Clear any previously-seen match for (category, parameter).

        Call this immediately before writing a command, so a stale echo from
        an earlier, unrelated action can't satisfy the upcoming `wait_for`.
        """
        key = (category, parameter)
        self._latest.pop(key, None)
        self._events[key] = asyncio.Event()

    async def wait_for(
        self, category: int, parameter: int, timeout: float
    ) -> tuple[CommandHeader, bytes] | None:
        """Await a fresh match for (category, parameter). `None` on timeout."""
        key = (category, parameter)
        event = self._events.setdefault(key, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return None
        return self._latest.get(key)
