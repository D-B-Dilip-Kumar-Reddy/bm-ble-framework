"""
Download one existing clip from the camera to the local PC —
`RestCameraSession.download_clip()` (Phase 12).

Unlike `delete_clip()`/`format_device()`, downloading is non-destructive:
nothing on the card changes. So this script targets whatever clip already
happens to be on the card (by default the first entry `clips()` reports)
rather than recording a disposable one first the way `rest_delete_clip.py`
does before its own destructive operation.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: nothing. It only writes a local
file at `DEST_DIR/<remote filename>`.

SEQUENCE: `clips()` -> pick a clip (`CLIP_UNIQUE_ID`, or the first one
found) -> `download_clip()`, which resolves the clip's real `/mounts/...`
path the same way `delete_clip()` does (`resolve_active_mount()` +
`file_path`'s basename) and streams it to disk via
`RestClient.download()`.

STATUS: `RestClient.download()`'s streaming/`Content-Length`-integrity
logic and `download_clip()`'s clip-resolution/mount-path composition are
both new this session, unit-tested against a fake client only — not yet
run against real hardware. This script's first successful run is that
confirmation, the same status every write/read-verb capability in this
codebase carries before its first real-hardware pass.

Edit HOST / MODEL_KEY / FIRMWARE / DEST_DIR / CLIP_UNIQUE_ID below to
target a different camera, destination, or clip.

Usage:
    python examples/rest_download_clip.py
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

from bmd_camera import RestCameraSession
from bmd_camera.exceptions import BMDStorageError, BMDVerificationError

HOST = "pocket-cinema-camera-6k-g2.local"
MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v8.6"
# HOST = "pocket-cinema-camera-6k-pro.local"
# MODEL_KEY = "POCKET_6K_PRO"
# FIRMWARE = "v8.6"

DEST_DIR = Path(__file__).parent / "downloads"
CLIP_UNIQUE_ID: int | None = None  # None = download the first clip clips() reports


def _format_bytes(num_bytes: int) -> str:
    gb = num_bytes / 1_000_000_000
    return f"{num_bytes} bytes ({gb:.2f} GB)"


async def main() -> int:
    DEST_DIR.mkdir(parents=True, exist_ok=True)

    async with RestCameraSession(HOST, MODEL_KEY, FIRMWARE) as session:
        try:
            clips = await session.clips()
        except BMDStorageError as exc:
            print(f"clips() failed: {exc}")
            return 1
        if not clips:
            print("No clips on the card — nothing to download.")
            return 1

        clip_unique_id = CLIP_UNIQUE_ID
        if clip_unique_id is None:
            clip_unique_id = clips[0].clip_unique_id
            print(f"CLIP_UNIQUE_ID not set — downloading the first clip: {clip_unique_id}")

        print(f"\n=== Downloading clip_unique_id={clip_unique_id} -> {DEST_DIR} ===")
        start = time.monotonic()
        try:
            dest = await session.download_clip(clip_unique_id, DEST_DIR, overwrite=True)
        except (ValueError, FileExistsError, BMDVerificationError) as exc:
            print(f"download_clip({clip_unique_id}) failed: {exc}")
            return 1
        elapsed = time.monotonic() - start

        size = dest.stat().st_size
        rate_mb_s = (size / 1_000_000) / elapsed if elapsed > 0 else 0.0
        print(f"download_clip({clip_unique_id}) confirmed ✓")
        print(f"  {dest}")
        print(f"  {_format_bytes(size)} in {elapsed:.1f}s ({rate_mb_s:.1f} MB/s)")

    return 0


if __name__ == "__main__":
    # Redirected/piped stdout is fully buffered by default in CPython, while
    # logging's StreamHandler flushes per record — without this, captured
    # log files interleave print() and logging output out of chronological
    # order, making timing hard to trust when diagnosing issues later.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
