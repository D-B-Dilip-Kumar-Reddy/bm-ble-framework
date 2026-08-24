"""
Download several existing clips from the camera to the local PC in one
batch — `RestCameraSession.download_clips()` (Phase 15), the mirror-image
operation of `delete_clips()` (Phase 13).

Unlike `delete_clips()`, downloading is non-destructive: nothing on the
card changes. So this script targets whatever clips already happen to be
on the card (by default the first `MAX_CLIPS` entries `clips()` reports)
rather than recording disposable ones first the way `rest_delete_clips_bulk.py`
does before its own destructive operation.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: nothing. It only writes local
files at `DEST_DIR/<remote filename>`.

SEQUENCE: `clips()` -> pick clips (`CLIP_UNIQUE_IDS`, or the first
`MAX_CLIPS` found) -> `download_clips()`, which validates every id against
one fresh `clips()` call and `DEST_DIR`'s existence before downloading
anything, then downloads each in turn via `download_clip()` — one clip's
failure doesn't stop the batch.

Reports both per-clip and aggregate throughput (total bytes / total
elapsed time) — useful as a rough measure of the SD card's real read
speed over this transport, since every byte here comes off the card
through the camera's own USB/network stack, not a synthetic benchmark.

STATUS: `download_clips()` is new this session, unit-tested against a
fake client only — not yet run against real hardware. This script's first
successful run is that confirmation.

Edit HOST / MODEL_KEY / FIRMWARE / DEST_DIR / CLIP_UNIQUE_IDS / MAX_CLIPS
below to target a different camera, destination, or clip set.

Usage:
    python examples/rest_download_clips_bulk.py
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

from bmd_camera import RestCameraSession
from bmd_camera.exceptions import BMDStorageError

HOST = "pocket-cinema-camera-6k-g2.local"
MODEL_KEY = "POCKET_6K_G2"
FIRMWARE = "v8.6"
# HOST = "pocket-cinema-camera-6k-pro.local"
# MODEL_KEY = "POCKET_6K_PRO"
# FIRMWARE = "v8.6"

DEST_DIR = Path(__file__).parent / "downloads"
CLIP_UNIQUE_IDS: list[int] | None = None  # None = download the first MAX_CLIPS clips() reports
MAX_CLIPS = 3


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

        clip_unique_ids = CLIP_UNIQUE_IDS
        if clip_unique_ids is None:
            clip_unique_ids = [c.clip_unique_id for c in clips[:MAX_CLIPS]]
            print(
                f"CLIP_UNIQUE_IDS not set — downloading the first {len(clip_unique_ids)}: "
                f"{clip_unique_ids}"
            )

        print(f"\n=== Downloading {clip_unique_ids} -> {DEST_DIR} ===")
        start = time.monotonic()
        result = await session.download_clips(clip_unique_ids, DEST_DIR, overwrite=True)
        elapsed = time.monotonic() - start

        total_bytes = 0
        for clip_unique_id, dest in result.downloaded:
            size = dest.stat().st_size
            total_bytes += size
            print(f"  clip_unique_id={clip_unique_id}: {dest} — {_format_bytes(size)}")

        if result.failed:
            print("\nFAILED:")
            for clip_unique_id, exc in result.failed:
                print(f"  clip_unique_id={clip_unique_id}: {exc}")

        rate_mb_s = (total_bytes / 1_000_000) / elapsed if elapsed > 0 else 0.0
        print(
            f"\n{len(result.downloaded)}/{len(clip_unique_ids)} downloaded, "
            f"{len(result.failed)} failed"
        )
        print(
            f"Total: {_format_bytes(total_bytes)} in {elapsed:.1f}s "
            f"({rate_mb_s:.1f} MB/s aggregate)"
        )

    return 1 if result.failed else 0


if __name__ == "__main__":
    # Redirected/piped stdout is fully buffered by default in CPython, while
    # logging's StreamHandler flushes per record — without this, captured
    # log files interleave print() and logging output out of chronological
    # order, making timing hard to trust when diagnosing issues later.
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    raise SystemExit(asyncio.run(main()))
