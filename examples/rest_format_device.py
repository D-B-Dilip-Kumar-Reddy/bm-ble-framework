"""
Format a media device — entirely over REST, no BLE involved. This is the
only media-erasure capability the official BMD REST spec exposes at all
(Phase 10): there is no per-clip or per-still delete endpoint in any of
the 11 official spec files this codebase has been given —
`TimelineControl.yaml`'s `DELETE /timelines/0` only clears the timeline
*object* (already implemented via `select_clip()`), never touching clip
files on disk. Whether the separate `/mounts/...` filesystem surface
supports `DELETE` for individual files is a genuinely open question, out
of scope here and deferred on request — see CLAUDE.md's Phase 10 note.

**THIS ERASES EVERY CLIP AND STILL ON THE TARGET DEVICE, IRREVERSIBLY.**
Because of that, this script layers two safety gates on top of
`RestCameraSession.format_device()`'s own mandatory `confirm=True`
argument, deliberately departing from every other examples/ script's
plain "edit the constants and run it" convention:

  1. Prints `storage_state()` first, so the operator can see exactly what
     is on the device before doing anything else.
  2. Requires typing the *exact* `DEVICE_NAME` back at a prompt — not just
     "yes" — before `format_device()` is ever called. Ctrl-C or any other
     input aborts with nothing sent to the camera.

`format_device()`'s own verification is structurally weaker than every
other write in this codebase: the official spec's `Notification.yaml`
documents no WS-subscribable property for any `/media/devices/...` path,
so there is no event to arm/wait for at all (design principle 3's dual
check cannot apply here) — the only signal is polling
`device_info(device_name).state` until a `"Formatting"` state has been
seen and then left. See `format_device()`'s own docstring for the full
reasoning, including why its capability check gates on `supported`
rather than `put_supported` (this endpoint is in
`tools/rest/probe_endpoints.py`'s `NEVER_WRITE` list, so `put_supported`
can never be sweep-confirmed).

STATUS: not yet real-hardware-confirmed. This script's first successful
run against real hardware *is* that confirmation — see
docs/rest/session.md's `format_device()` section.

Edit HOST / MODEL_KEY / FIRMWARE / DEVICE_NAME below to target a
different camera or device. FILESYSTEM / VOLUME are optional — leave
them None to keep the device's current filesystem and volume name.

Usage:
    python examples/rest_format_device.py
"""

import asyncio
import logging
import sys

from bmd_camera import BMDUnsupportedError, BMDVerificationError, RestCameraSession

HOST = "pocket-cinema-camera-6k-pro.local"
MODEL_KEY = "POCKET_6K_PRO"
FIRMWARE = "v8.6"
# HOST = "pocket-cinema-camera-6k-g2.local"
# MODEL_KEY = "POCKET_6K_G2"
# FIRMWARE = "v8.6"

# StorageDevice.device_name (e.g. "sd0"), not a /mounts/ mount name — read
# it off the storage_state() printout below before confirming.
DEVICE_NAME = "sd0"
FILESYSTEM: str | None = None  # e.g. "ExFat" — None keeps the current filesystem
VOLUME: str | None = None  # e.g. "My disk" — None keeps the current volume name

FORMAT_TIMEOUT_S = 120.0
FORMAT_POLL_INTERVAL_S = 1.0


def _format_bytes(num_bytes: int) -> str:
    gb = num_bytes / 1_000_000_000
    return f"{num_bytes} bytes ({gb:.2f} GB)"


async def _print_storage(session: RestCameraSession) -> None:
    storage = await session.storage_state()
    if not storage.devices:
        print("  No media devices reporting.")
        return
    for device in storage.devices:
        active = " (active)" if storage.active_device is device else ""
        print(f"  device={device.device_name!r} volume={device.volume!r}{active}")
        print(f"    total space:     {_format_bytes(device.total_space)}")
        print(f"    remaining space: {_format_bytes(device.remaining_space)}")
        print(f"    clip count:      {device.clip_count}")


def _confirm_by_typing_device_name() -> bool:
    print(
        f"\nThis will PERMANENTLY ERASE every clip and still on device "
        f"{DEVICE_NAME!r} ({HOST}). This cannot be undone."
    )
    answer = input(f"Type the device name ({DEVICE_NAME!r}) to proceed, anything else aborts: ")
    return answer == DEVICE_NAME


async def main() -> int:
    async with RestCameraSession(HOST, MODEL_KEY, FIRMWARE) as session:
        print("--- Media devices before formatting ---")
        await _print_storage(session)

        if not _confirm_by_typing_device_name():
            print("Aborted — device name did not match, nothing was sent to the camera.")
            return 1

        print(f"\n=== Formatting {DEVICE_NAME!r} ===")
        try:
            await session.format_device(
                DEVICE_NAME,
                confirm=True,
                filesystem=FILESYSTEM,
                volume=VOLUME,
                timeout=FORMAT_TIMEOUT_S,
                poll_interval_s=FORMAT_POLL_INTERVAL_S,
            )
        except (BMDUnsupportedError, BMDVerificationError) as exc:
            print(f"format_device({DEVICE_NAME!r}) NOT confirmed: {exc}")
            return 1
        print(f"format_device({DEVICE_NAME!r}) confirmed ✓")

        print("\n--- Media devices after formatting ---")
        await _print_storage(session)

    return 0


if __name__ == "__main__":
    # Redirected/piped stdout is fully buffered by default in CPython, while
    # logging's StreamHandler flushes per record — without this, captured
    # log files interleave print() and logging output out of chronological
    # order, making timing hard to trust when diagnosing issues later.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
