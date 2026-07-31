"""
tools/common/discovery.py
=========================
Pure logic for the guided command-discovery workflow
(tools/control/discover_command.py): candidate-command generation, seeding
from saved passive captures, echo extraction, and emitting a profile
``commands`` block in the payloads/schema.json shape.

Everything here is side-effect free and fully unit-tested
(tests/unit/tools/common/test_discovery.py) — no BLE, no ``input()``, no
filesystem access. The interactive driver in tools/control/ owns all I/O.

See docs/command_discovery.md for the workflow this supports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from bmd_ble.protocol.codec import Operation, encode_assign, encode_assign_void
from bmd_ble.protocol.types import DataType

# Characteristic name used by tools/common/capture.py's saved JSON for the
# incoming control characteristic (from bmd_ble.constants.CHARACTERISTIC_NAMES).
INCOMING_CONTROL_NAME = "INCOMING_CONTROL (Indicate)"


@dataclass(frozen=True)
class CandidateCommand:
    """One candidate command packet in a discovery sweep.

    ``value`` is ``None`` exactly when ``data_type`` is VOID: a void trigger
    has no payload to sweep, so the candidate is fully determined by its
    coordinates and reserved byte.
    """

    category: int
    parameter: int
    data_type: DataType
    value: int | None
    reserved: int

    def __post_init__(self) -> None:
        if (self.value is None) != (self.data_type is DataType.VOID):
            raise ValueError(
                "CandidateCommand.value must be None for a VOID candidate "
                "and an int for every other data type"
            )

    def encode(self) -> bytes:
        if self.value is None:
            return encode_assign_void(
                category=self.category,
                parameter=self.parameter,
                reserved=self.reserved,
            )
        return encode_assign(
            category=self.category,
            parameter=self.parameter,
            data_type=self.data_type,
            value=self.value,
            reserved=self.reserved,
        )

    def describe(self) -> str:
        payload = "void (no payload)" if self.value is None else f"value={self.value}"
        return (
            f"category=0x{self.category:02X} parameter=0x{self.parameter:02X} "
            f"{self.data_type.name} {payload} reserved=0x{self.reserved:02X}"
        )


def generate_candidates(
    *,
    category: int,
    parameter: int,
    data_type: DataType,
    values: list[int],
    reserveds: list[int],
) -> list[CandidateCommand]:
    """Deterministic sweep: for each reserved byte, each payload value, in
    the order given — the operator's ordering is meaningful (put the most
    likely candidate first so the camera spends the least time in odd
    states).

    A VOID data type has no payload axis: ``values`` must be empty and the
    sweep is one candidate per reserved byte.
    """
    if data_type is DataType.VOID:
        if values:
            raise ValueError(
                "A VOID (trigger) sweep takes no payload values — pass an empty values list."
            )
        return [
            CandidateCommand(
                category=category,
                parameter=parameter,
                data_type=data_type,
                value=None,
                reserved=reserved,
            )
            for reserved in reserveds
        ]
    return [
        CandidateCommand(
            category=category,
            parameter=parameter,
            data_type=data_type,
            value=value,
            reserved=reserved,
        )
        for reserved in reserveds
        for value in values
    ]


def _is_state_report(
    triple: tuple[int, int, str],
    per_window_payloads: list[dict[tuple[int, int, str], set[str]]],
) -> bool:
    """True when a triple present in every window still looks like a *state
    report* rather than ambient telemetry.

    The distinguishing shape: the payload is **stable within** each window but
    **differs between** windows. That is what a command family reporting state
    looks like — the camera settles on one value per operator action and holds
    it — whereas ambient telemetry (a storage counter, a timecode tick) keeps
    changing inside a single window.

    Only meaningful for a triple that appears in every window; the caller
    guarantees that, so each window's payload set here is non-empty.
    """
    payload_sets = [window[triple] for window in per_window_payloads]
    if any(len(payloads) != 1 for payloads in payload_sets):
        return False
    distinct_across_windows = {next(iter(payloads)) for payloads in payload_sets}
    return len(distinct_across_windows) > 1


def seed_triples_from_capture(
    capture: dict, *, exclude_ambient: bool = True
) -> list[tuple[int, int, str]]:
    """Extract candidate (category, parameter, data_type_name) triples from a
    saved tools/common/capture.py JSON (the dict shape `save_capture` writes).

    Only INCOMING_CONTROL notifications that decoded cleanly are considered —
    a camera-originated report for a (category, parameter) is the best seed
    for what command coordinates the camera will accept.

    With ``exclude_ambient`` (default), a triple present in *every* window is
    dropped as ambient telemetry (e.g. categories 0x09/0x0C ticking ~1/s on
    POCKET_6K_G2 v7.9) — **unless** its payloads mark it as a state report,
    per ``_is_state_report``.

    That exception is not a nicety. A start/stop command family reports the
    same (category, parameter) in *both* windows by construction, so presence
    alone dropped the very triple the sweep exists to find: on
    POCKET_6K_G2 v8.6 (2026-07-29) the recording family (0x0A/0x01) was
    filtered out of its own capture and every candidate the tool did offer was
    a dead end. Its payloads are the giveaway — exactly one per window,
    ``02 00 40 00 01 03`` in record_start and ``00 00 40 00 01 03`` in
    record_stop — while the genuine telemetry alongside it (0x09/0x00,
    0x09/0x02) took several different values inside each window.

    The filter is a no-op for single-window captures — capture at least two
    windows (e.g. record_start and record_stop) for it to bite.
    """
    windows = capture.get("windows", [])
    per_window: list[dict[tuple[int, int, str], set[str]]] = []
    ordered: list[tuple[int, int, str]] = []

    for window in windows:
        seen: dict[tuple[int, int, str], set[str]] = {}
        for notification in window.get("notifications", []):
            if notification.get("characteristic_name") != INCOMING_CONTROL_NAME:
                continue
            if notification.get("decode_error"):
                continue
            category = notification.get("category")
            parameter = notification.get("parameter")
            data_type = notification.get("data_type")
            if category is None or parameter is None or data_type is None:
                continue
            triple = (category, parameter, data_type)
            seen.setdefault(triple, set()).add(notification.get("payload_hex") or "")
            if triple not in ordered:
                ordered.append(triple)
        per_window.append(seen)

    if exclude_ambient and len(per_window) > 1:
        in_every_window = set.intersection(*(set(window) for window in per_window))
        ambient = {triple for triple in in_every_window if not _is_state_report(triple, per_window)}
        ordered = [triple for triple in ordered if triple not in ambient]

    return ordered


@dataclass(frozen=True)
class ConfirmedOutcome:
    """One operator-confirmed (outcome name, candidate) pair with its echo."""

    outcome: str
    candidate: CandidateCommand
    echo_operation: int | None = None
    echo_payload_hex: str | None = None


def extract_echo(
    notifications: list[dict], *, category: int, parameter: int
) -> tuple[int | None, str | None]:
    """First cleanly-decoded INCOMING_CONTROL notification matching
    (category, parameter), as (operation_int, payload_hex) — (None, None)
    when no echo decoded. Notifications are the dicts from a CaptureWindow
    (dataclasses.asdict shape).

    The operation arrives as an Operation name string in decoded captures;
    it is mapped back to its integer so the emitted profile block stores the
    raw byte (see payloads/schema.json's echo_operation rationale).
    """
    for notification in notifications:
        if notification.get("characteristic_name") != INCOMING_CONTROL_NAME:
            continue
        if notification.get("decode_error"):
            continue
        if notification.get("category") != category or notification.get("parameter") != parameter:
            continue
        operation_name = notification.get("operation")
        operation = (
            Operation[operation_name].value if operation_name in Operation.__members__ else None
        )
        return operation, notification.get("payload_hex")
    return None, None


def build_command_block(
    *,
    name: str,
    confirmed: list[ConfirmedOutcome],
    capture_ref: str | None,
    discovered_on: str,
) -> dict:
    """Assemble a payloads/schema.json-shaped ``commands`` block from
    operator-confirmed outcomes.

    Raises ValueError when outcomes disagree on the command coordinates
    (category/parameter/data_type/reserved), share a payload value, or reuse
    an outcome name — one block describes exactly one command family.
    """
    if not confirmed:
        raise ValueError("No confirmed outcomes — nothing to build a command block from.")

    first = confirmed[0].candidate
    for outcome in confirmed[1:]:
        candidate = outcome.candidate
        coordinates = (
            candidate.category,
            candidate.parameter,
            candidate.data_type,
            candidate.reserved,
        )
        if coordinates != (first.category, first.parameter, first.data_type, first.reserved):
            raise ValueError(
                f"Confirmed outcomes disagree on command coordinates: "
                f"'{confirmed[0].outcome}' used {first.describe()} but "
                f"'{outcome.outcome}' used {candidate.describe()} — "
                f"a command block describes exactly one (category, parameter, "
                f"data_type, reserved) family."
            )

    # For a VOID family every candidate's value is None (enforced by
    # CandidateCommand) and the block carries no `values` map at all — the
    # schema treats it as optional, same as the multi-element families.
    seen_outcomes: list[str] = []
    values: dict[str, int] = {}
    for outcome in confirmed:
        if outcome.outcome in seen_outcomes:
            raise ValueError(f"Outcome '{outcome.outcome}' confirmed more than once.")
        seen_outcomes.append(outcome.outcome)
        if outcome.candidate.value is None:
            continue
        if outcome.candidate.value in values.values():
            raise ValueError(
                f"Payload value {outcome.candidate.value} is confirmed for two outcomes — "
                f"each outcome needs a distinct value."
            )
        values[outcome.outcome] = outcome.candidate.value

    echo_operations = {o.echo_operation for o in confirmed if o.echo_operation is not None}
    echo_operation = echo_operations.pop() if len(echo_operations) == 1 else None

    block: dict = {
        "category": first.category,
        "parameter": first.parameter,
        "data_type": first.data_type.name,
        "reserved": first.reserved,
    }
    if values:
        block["values"] = values
    block["provenance"] = {
        "status": "VERIFIED",
        "method": "guided-discovery (tools/control/discover_command.py)",
        "capture_refs": [capture_ref] if capture_ref else [],
        "verified_on": discovered_on,
        "notes": f"Operator-confirmed outcomes: {', '.join(seen_outcomes)}.",
    }
    if echo_operation is not None:
        block["echo_operation"] = echo_operation
    return block


def render_profile_snippet(name: str, block: dict) -> str:
    """Ready-to-paste JSON for the profile's ``commands`` map."""
    return json.dumps({name: block}, indent=2)
