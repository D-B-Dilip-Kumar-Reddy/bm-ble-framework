"""
tools/control/verify_photo_trigger.py
=========================================
Cross-check a candidate BLE photo-trigger command against REST's Stills-
directory `mtime` signal — the independent, on-camera evidence
`discover_command.py`'s own operator-confirmation flow asks for when no BLE
echo exists to fall back on (`docs/ble/command_discovery.md`).

WHY THIS EXISTS
-----------------
The photo trigger (category `0x0A` / parameter `0x03` / VOID) has never
produced a BLE echo on any camera tested (`docs/ble/photo_capture.md` §7,
§9), so `discover_command.py`'s operator-confirmation UI is the only
verification available for it — and that tool's own warning is explicit:
confirming several different candidate byte values identically for the
same outcome is what an *unreliable read* looks like, not automatic proof
the camera ignores the payload. On `POCKET_6K_G2 v8.6`
(`tools/captures/POCKET_6K_G2_v8.6/POCKET_6K_G2_v8.6_20260804T123509.json`),
both `reserved=0x00` and `reserved=0x01` were confirmed `photo_taken` by a
quick operator glance, with no independent check between them — exactly
the ambiguous case that warning describes (`docs/ble/photo_capture.md`
§11.4).

This tool replaces the operator's glance with the same real per-photo
signal Phase 6 already built and repeatedly confirmed on hardware:
`rest/media.py`'s `stills_marker()`/`wait_for_new_still()`, which detects a
still actually landing on the card via the Stills subdirectory's own
`mtime`, independent of BLE entirely. For each `--reserved` value given, it
records a REST baseline, sends that one candidate over BLE, and reports a
real CONFIRMED/not-confirmed — no guessing, and no reliance on an echo that
this command has never produced.

WHAT THIS SCRIPT CHANGES ON THE CAMERA: takes one real photo per
`--reserved` value tested. Requires active storage. Uses the same physical
camera over both BLE (`--model-key`/`--firmware`, for scanning by
`ble_name`) and REST (`--host`) — see `examples/capture_photo.py` for the
same dual-transport pattern.

Usage — resolve the exact ambiguity this tool was written for:
    python tools/control/verify_photo_trigger.py \\
        --model-key POCKET_6K_G2 --firmware v8.6 \\
        --category 0x0A --parameter 0x03 --reserved 0x00,0x01 \\
        --host pocket-cinema-camera-6k-g2.local
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from capture import (  # noqa: E402
    CaptureSession,
    configure_console_logging,
    run_send_and_capture,
    save_capture,
)
from discovery import CandidateCommand, generate_candidates  # noqa: E402

from bmd_camera.ble.camera_controller import BMDCameraController  # noqa: E402
from bmd_camera.ble.protocol.types import DataType  # noqa: E402
from bmd_camera.ble.scanner import scan_for_camera  # noqa: E402
from bmd_camera.camera_profile import CameraProfile  # noqa: E402
from bmd_camera.rest.media import (  # noqa: E402
    resolve_active_mount,
    stills_marker,
    wait_for_new_still,
)
from bmd_camera.rest.session import RestCameraSession  # noqa: E402


async def prompt(text: str) -> str:
    """Async input() — same executor pattern as discover_command.py's."""
    loop = asyncio.get_running_loop()
    return (await loop.run_in_executor(None, input, text)).strip()


def parse_int_list(raw: str, flag: str) -> list[int]:
    try:
        return [int(part, 0) for part in raw.split(",") if part.strip() != ""]
    except ValueError as exc:
        raise SystemExit(f"{flag}: expected comma-separated integers, got {raw!r}") from exc


async def verify_one(
    ble_profile: CameraProfile,
    rest_session: RestCameraSession,
    mount_path: str,
    candidate: CandidateCommand,
    *,
    scan_timeout: float,
    listen_seconds: float,
    confirm_timeout: float,
) -> tuple[bool, CaptureSession]:
    """Send one candidate over BLE, cross-check via REST. Returns
    (confirmed, capture) — confirmed is True iff `wait_for_new_still()`
    observed a real Stills-directory change, never an operator's read."""
    baseline = await stills_marker(rest_session, mount_path)
    print(f"  Baseline Stills mtime: {baseline}")

    discovered = await scan_for_camera(ble_profile.ble_name, timeout=scan_timeout)
    cam = BMDCameraController(discovered=discovered, profile=ble_profile)
    await cam.connect()
    try:
        capture = await run_send_and_capture(
            cam, [(candidate.describe(), candidate.encode())], listen_seconds=listen_seconds
        )
    finally:
        await cam.disconnect()

    confirmed = await wait_for_new_still(
        rest_session, mount_path, baseline, timeout_s=confirm_timeout
    )
    return confirmed, capture


async def main() -> int:
    args = parse_args()
    category: int = args.category
    parameter: int = args.parameter
    reserveds = parse_int_list(args.reserved, "--reserved")

    candidates = generate_candidates(
        category=category,
        parameter=parameter,
        data_type=DataType.VOID,
        values=[],
        reserveds=reserveds,
    )

    print(f"\nWill test {len(candidates)} candidate(s) against {args.model_key}, each")
    print("cross-checked via REST's Stills-directory mtime — not an operator glance:")
    for i, candidate in enumerate(candidates, start=1):
        tx = " ".join(f"{b:02X}" for b in candidate.encode())
        print(f"  [{i}] {candidate.describe()}  ->  TX: {tx}")
    print(
        "\nEach candidate takes one real photo if it works. Storage must be ready on the "
        "\nsame physical camera reachable at both --model-key/--firmware (BLE) and --host (REST)."
    )
    answer = await prompt("Type 'yes' to proceed: ")
    if answer.lower() != "yes":
        print("Aborted before any write.")
        return 1

    ble_profile = CameraProfile.for_model(model_key=args.model_key, firmware=args.firmware)

    results: dict[int, bool] = {}
    async with RestCameraSession(
        args.host, args.model_key, args.firmware, scheme=args.scheme, port=args.port
    ) as rest_session:
        storage = await rest_session.storage_state()
        if storage.active_device is None:
            raise SystemExit(f"[{args.host}] No active storage device — cannot verify a trigger")
        mount_path = await resolve_active_mount(rest_session)
        print(f"Resolved mount: {mount_path}")

        for i, candidate in enumerate(candidates, start=1):
            print(f"\n--- [{i}/{len(candidates)}] {candidate.describe()}")
            confirmed, capture = await verify_one(
                ble_profile,
                rest_session,
                mount_path,
                candidate,
                scan_timeout=args.scan_timeout,
                listen_seconds=args.listen_seconds,
                confirm_timeout=args.confirm_timeout,
            )
            results[candidate.reserved] = confirmed
            print(f"  REST result: {'CONFIRMED' if confirmed else 'NOT confirmed'}")
            saved_path = save_capture(args.model_key, args.firmware, capture)
            print(f"  Capture evidence saved to: {saved_path}")
            if i < len(candidates):
                await prompt(
                    "Restore the camera to a safe idle state if needed, then press Enter... "
                )

    print(summarize_results(category, parameter, results))

    confirmed_reserveds = sorted(r for r, ok in results.items() if ok)
    return 0 if confirmed_reserveds else 1


def summarize_results(category: int, parameter: int, results: dict[int, bool]) -> str:
    """The final report text — pure, so it's testable without a live camera.
    `results` maps each tested `reserved` byte to whether REST confirmed a
    real Stills-directory change for it (never an operator's read)."""
    lines = ["\n" + "=" * 78, "RESULTS — REST-confirmed, not operator-glanced:"]
    for reserved, confirmed in results.items():
        lines.append(
            f"  reserved=0x{reserved:02X}: {'CONFIRMED' if confirmed else 'not confirmed'}"
        )

    confirmed_reserveds = sorted(r for r, ok in results.items() if ok)
    if len(confirmed_reserveds) == 1:
        r = confirmed_reserveds[0]
        lines.append(
            f"\nExactly one candidate REST-confirmed — paste this into the profile's "
            f"'photo' block:\n"
            f"  category=0x{category:02X} parameter=0x{parameter:02X} VOID reserved=0x{r:02X}"
        )
    elif len(confirmed_reserveds) > 1:
        lines.append(
            "\nMore than one candidate was REST-confirmed — this IS now genuine evidence the "
            "camera ignores the reserved byte for this command (not an unreliable read this "
            "time, since each answer came from an independent on-card signal, not a glance). "
            "Record this explicitly in provenance.notes, and pick one value for the profile "
            "block — e.g. the one already used on this camera's other firmware or on other "
            f"cameras' 'photo' blocks, for consistency (reserved=0x{confirmed_reserveds[0]:02X} "
            "is the lowest of the confirmed set)."
        )
    else:
        lines.append(
            "\nNo candidate was REST-confirmed. Re-check storage/mount state and try again."
        )

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cross-check candidate photo-trigger commands against REST's Stills-directory "
            "mtime signal instead of an operator's glance. WILL take real photos."
        )
    )
    parser.add_argument("--model-key", required=True, help="Camera model key, e.g. POCKET_6K_G2")
    parser.add_argument("--firmware", required=True, help="Camera firmware, e.g. v8.6")
    parser.add_argument(
        "--category", required=True, type=lambda s: int(s, 0), help="Command category byte"
    )
    parser.add_argument(
        "--parameter", required=True, type=lambda s: int(s, 0), help="Command parameter byte"
    )
    parser.add_argument(
        "--reserved",
        required=True,
        help="Comma-separated reserved-byte candidates to test, e.g. 0x00,0x01",
    )
    parser.add_argument(
        "--host",
        required=True,
        help="Camera hostname or IP for REST, e.g. pocket-cinema-camera-6k-g2.local. "
        "See docs/rest/transport.md for how to find this over USB.",
    )
    parser.add_argument(
        "--scheme",
        default="http",
        choices=["http", "https"],
        help="REST URL scheme. Default: http.",
    )
    parser.add_argument(
        "--port", type=int, default=None, help="REST port override, if not the scheme's default."
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=15.0,
        help="BLE scan timeout in seconds. Default: 15.0",
    )
    parser.add_argument(
        "--listen-seconds",
        type=float,
        default=3.0,
        help="Seconds to listen for a BLE response after each candidate. Default: 3.0",
    )
    parser.add_argument(
        "--confirm-timeout",
        type=float,
        default=15.0,
        help="Seconds to wait for REST to confirm a Stills-directory change. Default: 15.0",
    )
    return parser.parse_args()


if __name__ == "__main__":
    configure_console_logging()
    raise SystemExit(asyncio.run(main()))
