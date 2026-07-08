"""
tools/control/discover_command.py
==================================
Guided command discovery — reverse-engineer the exact command bytes a camera
accepts for an action whose command is not yet known, and emit a
ready-to-paste ``commands`` block for the profile JSON.

Unlike tools/control/send_record_command.py (which replays a command already
fully specified in the profile), this tool sends *unverified candidate*
commands to a real camera. It therefore requires an explicit typed
confirmation before the first write, and asks the operator to confirm what
the camera physically did after every candidate — the operator, not the
echo, is the ground truth here.

Workflow (see docs/command_discovery.md for the full writeup):

  A. Seed (category, parameter, data_type) — either from a saved passive
     capture (tools/sniffers/, --from-capture) or from CLI flags. The
     [spec] tables in docs/protocol.md are the map for choosing seeds.
  B. Generate the candidate sweep (--values × --reserved) and confirm it.
  C. For each candidate: send, capture the response for --listen-seconds,
     show the decoded packets, ask the operator what the camera did.
  D. Save the capture as evidence and print the profile JSON block.

Example — recording start/stop on the Pocket 6K Pro:

    python tools/sniffers/sniffer_recording.py --model-key POCKET_6K_PRO --firmware v8.6
    python tools/control/discover_command.py \
        --model-key POCKET_6K_PRO --firmware v8.6 \
        --label recording --from-capture tools/captures/POCKET_6K_PRO_v8.6/<file>.json \
        --values 2,0 --reserved 1,0 --outcomes start,stop

The tool never edits the profile JSON itself — paste the emitted block into
payloads/models/<MODEL_KEY>_<FIRMWARE>.json and run `pytest tests/unit`
(the schema tests validate it immediately).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "common"))

from capture import (  # noqa: E402
    CaptureSession,
    configure_console_logging,
    run_send_and_capture,
    save_capture,
)
from discovery import (  # noqa: E402
    CandidateCommand,
    ConfirmedOutcome,
    build_command_block,
    extract_echo,
    generate_candidates,
    render_profile_snippet,
    seed_triples_from_capture,
)

from bmd_ble.camera_controller import BMDCameraController  # noqa: E402
from bmd_ble.camera_profile import CameraProfile  # noqa: E402
from bmd_ble.protocol.types import DataType  # noqa: E402
from bmd_ble.scanner import scan_for_camera  # noqa: E402


async def prompt(text: str) -> str:
    """Async input() — same executor pattern as capture.run_capture_windows."""
    loop = asyncio.get_running_loop()
    return (await loop.run_in_executor(None, input, text)).strip()


def parse_int_list(raw: str, flag: str) -> list[int]:
    try:
        return [int(part, 0) for part in raw.split(",") if part.strip() != ""]
    except ValueError as exc:
        raise SystemExit(f"{flag}: expected comma-separated integers, got {raw!r}") from exc


async def resolve_seed(args: argparse.Namespace) -> tuple[int, int, DataType]:
    """Phase A: seed (category, parameter, data_type) from a capture or CLI."""
    if args.from_capture:
        capture = json.loads(Path(args.from_capture).read_text(encoding="utf-8"))
        triples = seed_triples_from_capture(capture)
        if not triples:
            raise SystemExit(
                f"No window-specific INCOMING_CONTROL triples found in {args.from_capture} — "
                f"capture at least two windows (the ambient filter needs contrast), or pass "
                f"--category/--parameter/--data-type manually."
            )
        print("\nCandidate (category, parameter, data_type) triples from the capture")
        print("(ambient telemetry seen in every window is already filtered out):")
        for i, (category, parameter, data_type) in enumerate(triples, start=1):
            print(f"  [{i}] category=0x{category:02X} parameter=0x{parameter:02X} {data_type}")
        while True:
            answer = await prompt(f"Pick a triple [1-{len(triples)}]: ")
            if answer.isdigit() and 1 <= int(answer) <= len(triples):
                category, parameter, data_type_name = triples[int(answer) - 1]
                return category, parameter, DataType[data_type_name]
            print("Invalid choice.")

    if args.category is None or args.parameter is None or args.data_type is None:
        raise SystemExit(
            "Seed the sweep with either --from-capture <saved sniffer JSON> "
            "or all of --category/--parameter/--data-type."
        )
    return args.category, args.parameter, DataType[args.data_type]


async def confirm_sweep(candidates: list[CandidateCommand], model_key: str) -> bool:
    """Phase B gate: show the full plan, require a typed 'yes'."""
    print(f"\nSweep plan — {len(candidates)} candidate command(s) will be SENT to {model_key}:")
    for i, candidate in enumerate(candidates, start=1):
        tx = " ".join(f"{b:02X}" for b in candidate.encode())
        print(f"  [{i}] {candidate.describe()}  ->  TX: {tx}")
    print(
        "\nThese are UNVERIFIED candidate commands. The camera may change state in"
        "\nunexpected ways. Keep the camera in view and be ready to intervene."
    )
    answer = await prompt("Type 'yes' to proceed: ")
    return answer.lower() == "yes"


async def probe_candidates(
    cam: BMDCameraController,
    candidates: list[CandidateCommand],
    outcomes: list[str],
    args: argparse.Namespace,
) -> tuple[list[ConfirmedOutcome], CaptureSession]:
    """Phase C: send each candidate, show the response, record the operator's
    confirmation of what the camera physically did."""
    confirmed: list[ConfirmedOutcome] = []
    combined = CaptureSession()
    remaining = list(outcomes)

    index = 0
    while index < len(candidates):
        candidate = candidates[index]
        label = f"candidate {candidate.describe()}"
        print(f"\n--- [{index + 1}/{len(candidates)}] Sending {label}")
        session = await run_send_and_capture(
            cam, [(label, candidate.encode())], listen_seconds=args.listen_seconds
        )
        combined.windows.extend(session.windows)
        # asdict-shaped dicts — the same shape save_capture writes and
        # discovery.extract_echo consumes.
        notifications = [asdict(n) for n in session.windows[-1].notifications]

        menu = "  ".join(f"[{i + 1}] {name}" for i, name in enumerate(outcomes))
        print("\nWhat did the camera physically do?")
        answer = await prompt(f"  {menu}  [n] nothing  [r] repeat  [q] quit sweep: ")

        if answer.lower() == "q":
            break
        if answer.lower() == "r":
            continue  # resend the same candidate
        if answer.isdigit() and 1 <= int(answer) <= len(outcomes):
            outcome = outcomes[int(answer) - 1]
            echo_operation, echo_payload = extract_echo(
                notifications, category=candidate.category, parameter=candidate.parameter
            )
            confirmed.append(
                ConfirmedOutcome(
                    outcome=outcome,
                    candidate=candidate,
                    echo_operation=echo_operation,
                    echo_payload_hex=echo_payload,
                )
            )
            if outcome in remaining:
                remaining.remove(outcome)
            print(f"Confirmed: '{outcome}' <- {candidate.describe()}")
            if echo_payload is not None:
                print(f"Echo: operation={echo_operation} payload={echo_payload}")
            else:
                print("No decodable echo captured — the operator confirmation stands on its own.")
            # The camera may now be in a changed state (e.g. recording).
            if args.restore_value is not None and candidate.value != args.restore_value:
                restore = CandidateCommand(
                    category=candidate.category,
                    parameter=candidate.parameter,
                    data_type=candidate.data_type,
                    value=args.restore_value,
                    reserved=candidate.reserved,
                )
                print(f"Sending --restore-value command: {restore.describe()}")
                restore_session = await run_send_and_capture(
                    cam,
                    [(f"restore {restore.describe()}", restore.encode())],
                    listen_seconds=args.listen_seconds,
                )
                combined.windows.extend(restore_session.windows)
            elif args.restore_value is None:
                await prompt(
                    "Restore the camera to a safe idle state (on the body if needed), "
                    "then press Enter... "
                )
            if not remaining:
                answer = await prompt(
                    "All requested outcomes confirmed. Continue sweeping anyway? [y/N]: "
                )
                if answer.lower() != "y":
                    break
        else:
            print("Treating as 'nothing observed'.")
        index += 1

    return confirmed, combined


async def run(args: argparse.Namespace) -> int:
    category, parameter, data_type = await resolve_seed(args)
    values = parse_int_list(args.values, "--values")
    reserveds = parse_int_list(args.reserved, "--reserved")
    outcomes = [name.strip() for name in args.outcomes.split(",") if name.strip()]
    if not outcomes:
        raise SystemExit("--outcomes must name at least one outcome (e.g. start,stop).")

    candidates = generate_candidates(
        category=category,
        parameter=parameter,
        data_type=data_type,
        values=values,
        reserveds=reserveds,
    )
    if not await confirm_sweep(candidates, args.model_key):
        print("Aborted before any write.")
        return 1

    profile = CameraProfile.for_model(model_key=args.model_key, firmware=args.firmware)
    discovered = await scan_for_camera(profile.ble_name, timeout=args.timeout)
    cam = BMDCameraController(discovered=discovered, profile=profile)

    await cam.connect()
    try:
        confirmed, combined = await probe_candidates(cam, candidates, outcomes, args)
        saved_path = save_capture(args.model_key, args.firmware, combined)
        print(f"\nCapture evidence saved to: {saved_path}")
    finally:
        await cam.disconnect()

    if not confirmed:
        print("\nNo outcomes were confirmed — nothing to emit.")
        return 1

    block = build_command_block(
        name=args.label,
        confirmed=confirmed,
        capture_ref=str(saved_path),
        discovered_on=date.today().isoformat(),
    )
    print(
        f"\nPaste this into payloads/models/{args.model_key}_{args.firmware}.json's"
        f' "commands" map, then run `python -m pytest tests/unit`:\n'
    )
    print(render_profile_snippet(args.label, block))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Guided command discovery: sends UNVERIFIED candidate commands to a real "
            "camera, asks the operator to confirm what happened, and emits a profile "
            "commands block. No defaults for --model-key/--firmware — be explicit "
            "about which camera you are probing."
        )
    )
    parser.add_argument("--model-key", required=True, help="Camera model key, e.g. POCKET_6K_PRO")
    parser.add_argument("--firmware", required=True, help="Camera firmware, e.g. v8.6")
    parser.add_argument(
        "--label",
        required=True,
        help="Command-family name for the emitted block, e.g. recording",
    )
    parser.add_argument(
        "--from-capture",
        help="Saved tools/sniffers capture JSON to seed (category, parameter, data_type) from",
    )
    parser.add_argument("--category", type=lambda s: int(s, 0), help="Manual seed: category byte")
    parser.add_argument("--parameter", type=lambda s: int(s, 0), help="Manual seed: parameter byte")
    parser.add_argument(
        "--data-type",
        choices=[t.name for t in DataType],
        help="Manual seed: payload data type name",
    )
    parser.add_argument(
        "--values",
        required=True,
        help="Comma-separated payload values to sweep, most likely first (e.g. 2,0)",
    )
    parser.add_argument(
        "--reserved",
        default="0",
        help=(
            "Comma-separated reserved-byte values to sweep. Default 0. "
            "Note POCKET_6K_G2 v7.9's real recording command needed 1 — "
            "try '1,0' when seeding from a G2-like camera."
        ),
    )
    parser.add_argument(
        "--outcomes",
        required=True,
        help="Comma-separated outcome names the operator can confirm (e.g. start,stop)",
    )
    parser.add_argument(
        "--listen-seconds",
        type=float,
        default=3.0,
        help="Seconds to listen for a response after each candidate. Default: 3.0",
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0, help="BLE scan timeout in seconds. Default: 15.0"
    )
    parser.add_argument(
        "--restore-value",
        type=lambda s: int(s, 0),
        default=None,
        help=(
            "Optional payload value to auto-send after each state-changing confirmation "
            "to return the camera to idle (e.g. the suspected stop value). Without it, "
            "the tool prompts for a manual restore instead."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    configure_console_logging()
    raise SystemExit(asyncio.run(run(parse_args())))
