"""
tools/common/capture.py
========================
Reusable BLE-notification capture engine shared by tools/sniffers/ (passive,
listen-only) and tools/control/ (active, sends a command then listens).

Two capture modes:
  - `run_capture_windows` — interactive, operator-triggered: for each labeled
    action, the operator is prompted to trigger it on the physical camera
    between two Enter presses.
  - `run_send_and_capture` — this repo's tooling sends the command itself
    (via `BMDCameraController.write_outgoing_control`) and listens for a
    fixed duration afterwards, for a deterministic, repeatable capture.

Either way, every INCOMING_CONTROL / CAMERA_STATUS notification received
during a window is decoded and reported, letting a feature script discover
which (characteristic, category, parameter) triples are associated with a
real camera action, without inventing protocol values (CLAUDE.md design
principle 6, "sniffer-first").

This module has no knowledge of any specific feature (recording, settings,
media, ...) — feature scripts supply only their own action labels (and, for
`run_send_and_capture`, the raw command bytes) and reuse
`print_window_summary` / `save_capture`. See docs/ble/sniffer_capture_engine.md
and docs/ble/active_camera_control.md for the full design writeup.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from bmd_camera.ble.camera_controller import BMDCameraController
from bmd_camera.ble.constants import CHARACTERISTIC_NAMES
from bmd_camera.ble.protocol.codec import decode_packet

CAPTURES_DIR = Path(__file__).resolve().parents[1] / "captures"


def configure_console_logging(level: int = logging.INFO) -> None:
    """Configure console logging so per-notification DEBUG hex dumps from
    BMDCameraController's default handlers (RX/TIMECODE/CAM_STATUS — always
    logged at DEBUG regardless of this level, since each controller instance
    pins its own logger to DEBUG) never reach the console and bury this
    tool's interactive Enter-to-continue prompts.

    Full detail still lands in the per-session file log camera_controller.py
    writes independently of this handler. Only the console handler's own
    level is narrowed here, since `basicConfig(level=...)` alone only gates
    calls made directly on the root logger — it does not filter records
    already accepted by a child logger with its own explicit level as they
    propagate up to ancestor handlers.
    """
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logging.basicConfig(level=level, handlers=[console_handler])


@dataclass(frozen=True)
class DecodedNotification:
    """Normalized view of one BLE notification captured during a window."""

    timestamp: str
    characteristic_uuid: str
    characteristic_name: str
    raw_hex: str
    category: int | None
    parameter: int | None
    data_type: str | None
    operation: str | None
    payload_hex: str | None
    decode_error: str | None


@dataclass
class CaptureWindow:
    """One labeled action window and the notifications observed during it."""

    label: str
    notifications: list[DecodedNotification] = field(default_factory=list)


@dataclass
class CaptureSession:
    """All windows captured in one run, plus which window is currently live."""

    windows: list[CaptureWindow] = field(default_factory=list)
    active: CaptureWindow | None = None


def _hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def decode_notification(characteristic: Any, data: bytearray) -> DecodedNotification:
    """Decode one raw BLE notification. Never raises.

    A `ValueError` from `decode_packet` (expected for CAMERA_STATUS, which is
    a raw status byte rather than a BMD command packet) is recorded in
    `decode_error`, not propagated — it is not a bug condition.
    """
    raw = bytes(data)
    uuid = str(getattr(characteristic, "uuid", "")).lower()
    name = CHARACTERISTIC_NAMES.get(uuid, f"UNKNOWN ({uuid})")
    timestamp = datetime.now().isoformat(timespec="milliseconds")

    try:
        header, payload = decode_packet(raw)
    except ValueError as exc:
        return DecodedNotification(
            timestamp=timestamp,
            characteristic_uuid=uuid,
            characteristic_name=name,
            raw_hex=_hex(raw),
            category=None,
            parameter=None,
            data_type=None,
            operation=None,
            payload_hex=None,
            decode_error=str(exc),
        )

    return DecodedNotification(
        timestamp=timestamp,
        characteristic_uuid=uuid,
        characteristic_name=name,
        raw_hex=_hex(raw),
        category=header.category,
        parameter=header.parameter,
        data_type=header.data_type.name,
        operation=header.operation.name,
        payload_hex=_hex(payload),
        decode_error=None,
    )


def dedupe_triples(
    notifications: list[DecodedNotification],
) -> list[tuple[str, int | None, int | None]]:
    """(characteristic_name, category, parameter) triples, first-seen order, no dupes."""
    return list(
        dict.fromkeys((n.characteristic_name, n.category, n.parameter) for n in notifications)
    )


def make_capture_callback(session: CaptureSession):
    """Bleak-style callback(characteristic, data) recording into the live window.

    Notifications are dropped when no window is currently active. `session`
    is a single shared object whose `.active` field the driving loop
    reassigns per window — safe because Bleak callbacks and the driving
    coroutine both run on the same event-loop thread.
    """

    def _callback(characteristic: Any, data: bytearray) -> None:
        if session.active is None:
            return
        session.active.notifications.append(decode_notification(characteristic, data))

    return _callback


def print_window_summary(window: CaptureWindow) -> None:
    """Print deduped triples followed by the full raw notification list."""
    print(f"\n=== Window: {window.label} ===")

    triples = dedupe_triples(window.notifications)
    print("Deduped triples (characteristic, category, parameter):")
    if not triples:
        print("  (none observed)")
    for name, category, parameter in triples:
        cat_str = f"0x{category:02X}" if category is not None else "None"
        param_str = f"0x{parameter:02X}" if parameter is not None else "None"
        print(f"  {name} | category={cat_str} | parameter={param_str}")

    print(f"\nFull packet list ({len(window.notifications)} notifications):")
    for n in window.notifications:
        suffix = f"  (decode_error: {n.decode_error})" if n.decode_error else ""
        print(f"  [{n.timestamp}] {n.characteristic_name} | {n.raw_hex}{suffix}")


async def _subscribe_capture_callback(cam: BMDCameraController, session: CaptureSession) -> None:
    """Subscribe the shared capture callback to INCOMING_CONTROL and CAMERA_STATUS.

    TIMECODE is deliberately not subscribed — it ticks roughly once a second
    regardless of any triggered action and would just be noise for "what
    changed because of this action". These are the same two characteristics
    CLAUDE.md's verification strategy checks (echo primary, CAMERA_STATUS
    secondary).
    """
    callback = make_capture_callback(session)
    await cam.subscribe_incoming(callback=callback)
    await cam.subscribe_camera_status(callback=callback)


async def run_capture_windows(cam: BMDCameraController, labels: list[str]) -> CaptureSession:
    """Run one interactive capture window per label and return the session.

    The operator triggers each action out-of-band (physical camera controls
    or another app); this function only listens. For a mode that sends the
    command itself, see `run_send_and_capture`.
    """
    session = CaptureSession()
    await _subscribe_capture_callback(cam, session)

    loop = asyncio.get_running_loop()

    async def wait_for_enter(prompt: str) -> None:
        await loop.run_in_executor(None, input, prompt)

    for label in labels:
        window = CaptureWindow(label=label)
        session.windows.append(window)

        await wait_for_enter(f"\n[{label}] Get ready, then press Enter to start capturing... ")
        session.active = window
        await wait_for_enter(f"[{label}] Trigger the action now. Press Enter when done... ")
        session.active = None

        print_window_summary(window)

    return session


async def run_send_and_capture(
    cam: BMDCameraController,
    actions: list[tuple[str, bytes]],
    *,
    listen_seconds: float = 3.0,
) -> CaptureSession:
    """Send each labeled command and capture whatever arrives afterwards.

    Unlike `run_capture_windows`, this repo's own tooling triggers the
    action — via `cam.write_outgoing_control(command_bytes)` — rather than
    waiting on an operator. Each window stays "hot" for `listen_seconds`
    after the write, then closes and prints its summary (explicitly showing
    "0 notifications" if nothing arrived, not silently succeeding).

    This actively sends commands to a real camera — see
    docs/ble/active_camera_control.md.
    """
    session = CaptureSession()
    await _subscribe_capture_callback(cam, session)

    for label, command in actions:
        window = CaptureWindow(label=label)
        session.windows.append(window)

        session.active = window
        await cam.write_outgoing_control(command)
        await asyncio.sleep(listen_seconds)
        session.active = None

        print_window_summary(window)

    return session


def save_capture(
    model_key: str,
    firmware: str,
    session: CaptureSession,
    *,
    captures_dir: Path = CAPTURES_DIR,
) -> Path:
    """Save the full-fidelity capture to JSON and return the saved path."""
    out_dir = captures_dir / f"{model_key}_{firmware}"
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{model_key}_{firmware}_{timestamp}.json"

    payload = {
        "model_key": model_key,
        "firmware": firmware,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "windows": [
            {
                "label": window.label,
                "notifications": [asdict(n) for n in window.notifications],
                "deduped_triples": [
                    {"characteristic_name": name, "category": category, "parameter": parameter}
                    for name, category, parameter in dedupe_triples(window.notifications)
                ],
            }
            for window in session.windows
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
