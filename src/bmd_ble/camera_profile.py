"""
bmd_ble/camera_profile.py
=====================
CameraProfile dataclass — the single source of truth for all
model/firmware-specific constants.

Every value that can differ between camera models or firmware versions lives
here and is loaded from payloads/models/<MODEL_KEY>_<FIRMWARE>.json, which is
validated against payloads/schema.json at load time.

DESIGN PRINCIPLE
────────────────
Nothing in the automation code should hardcode category/parameter pairs,
payload values, or BLE UUIDs.  Instead, obtain a CameraProfile for the target
camera and read from it:

    profile = CameraProfile.for_model("POCKET_6K_G2", "v7.9")
    spec = profile.require_command("recording", ("start", "stop"))
    spec.category                # → 10
    spec.values["start"]         # → 2
    uuid = profile.incoming_uuid # → "b864e140-..."

ADDING A NEW CAMERA
───────────────────
1. Create payloads/models/<MODEL_KEY>_<FIRMWARE>.json conforming to
   payloads/schema.json (see docs/payload_profiles.md).
2. Run the sniffer / discovery tools to populate verified values.
3. Add the model key to KNOWN_PROFILES below.
4. Run pytest — the parametrize fixtures pick up new profiles automatically.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import jsonschema

from .constants import CHARACTERISTIC_INCOMING
from .protocol.types import DataType

logger = logging.getLogger(__name__)

PAYLOADS_DIR = Path(__file__).resolve().parents[2] / "payloads"
MODELS_DIR = PAYLOADS_DIR / "models"
SCHEMA_PATH = PAYLOADS_DIR / "schema.json"

# Registry of all known (model_key, firmware) pairs.
# Add a new tuple here after creating the corresponding JSON file.
KNOWN_PROFILES: list[tuple[str, str]] = [
    ("POCKET_6K_G2", "v7.9"),
    ("POCKET_6K_PRO", "v8.6"),
]


@lru_cache(maxsize=1)
def load_schema() -> dict:
    """Load and cache payloads/schema.json."""
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def validate_profile(raw: dict, *, source: str) -> None:
    """Validate a raw profile dict against payloads/schema.json.

    Raises ValueError (wrapping jsonschema.ValidationError) whose message
    names the offending JSON path and the source file, so a typo like
    ``start_val`` fails loudly at load time instead of silently becoming a
    default.
    """
    try:
        jsonschema.validate(raw, load_schema())
    except jsonschema.ValidationError as exc:
        raise ValueError(
            f"Profile {source} failed schema validation at {exc.json_path}: {exc.message}"
        ) from exc


@dataclass(frozen=True)
class CommandProvenance:
    """Where a command block's values came from and how far they're trusted."""

    status: str  # VERIFIED | UNVERIFIED | CANDIDATE
    method: str | None = None
    capture_refs: tuple[str, ...] = ()
    verified_on: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class CommandSpec:
    """One sniffer-confirmed command family from a profile's ``commands`` map.

    ``values`` maps outcome names to the sniffer-captured payload integers,
    e.g. ``{"start": 2, "stop": 0}`` for recording.
    """

    name: str
    category: int
    parameter: int
    data_type: DataType
    values: dict[str, int]
    reserved: int = 0x00
    operation: int | None = None
    echo_operation: int | None = None
    provenance: CommandProvenance | None = None


@dataclass
class CameraProfile:
    """
    Complete resolved profile for one (model_key, firmware) pair.

    ``_meta.status`` describes the profile as a whole (CLAUDE.md design
    principle 8); each command block carries its own ``provenance.status``.
    """

    model_key: str
    model_name: str
    firmware: str
    status: str

    # BLE
    ble_name: str  # Default advertisement name from _meta.ble_name
    incoming_uuid: str  # To override default uuid in constants.py

    # GAP / Device info Metadata
    gap_metadata_readable: bool
    device_info_metadata_readable: bool

    # Sniffer-confirmed command families, keyed by command name.
    commands: dict[str, CommandSpec] = field(default_factory=dict)

    # Raw JSON for reference
    _raw: dict = field(default_factory=dict, repr=False, compare=False)

    # ── Command access ───────────────────────────────────────────────────────

    def command(self, name: str) -> CommandSpec | None:
        """The named command block, or None when this profile lacks it."""
        return self.commands.get(name)

    def require_command(self, name: str, value_names: tuple[str, ...] = ()) -> CommandSpec:
        """Return the named command block, raising ValueError when the block
        or any of ``value_names`` is missing from the profile JSON."""
        json_path = f"payloads/models/{self.model_key}_{self.firmware}.json"
        spec = self.commands.get(name)
        if spec is None:
            raise ValueError(
                f"Profile {self.model_key}_{self.firmware} has no '{name}' command block — "
                f"populate {json_path}'s commands.{name} first "
                f"(tools/control/discover_command.py can derive it from a capture)."
            )
        missing = [value for value in value_names if value not in spec.values]
        if missing:
            raise ValueError(
                f"Profile {self.model_key}_{self.firmware} command '{name}' is missing "
                f"values: {', '.join(missing)} — populate commands.{name}.values in {json_path}."
            )
        return spec

    # ── Construction ─────────────────────────────────────────────────────────

    @classmethod
    def for_model(cls, model_key: str, firmware: str) -> CameraProfile:
        """
        Load and return a CameraProfile for (model_key, firmware).

        Searches payloads/models/<MODEL_KEY>_<FIRMWARE>.json, validates it
        against payloads/schema.json, and cross-checks the file's _meta
        identity against the requested pair. Raises FileNotFoundError if the
        JSON does not exist and ValueError on validation failure. Logs a
        prominent warning when the profile is not VERIFIED (CLAUDE.md design
        principle 8).
        """
        filename = MODELS_DIR / f"{model_key}_{firmware}.json"
        if not filename.exists():
            raise FileNotFoundError(
                f"No model JSON found at {filename}. Create it conforming to payloads/schema.json."
            )
        with open(filename, encoding="utf-8") as fh:
            raw = json.load(fh)

        validate_profile(raw, source=filename.name)

        meta = raw.get("_meta", {})
        for key, expected in (("model_key", model_key), ("firmware", firmware)):
            actual = meta.get(key)
            if actual != expected:
                raise ValueError(
                    f"Profile {filename.name} declares _meta.{key}={actual!r} "
                    f"but was loaded as {expected!r} — filename and _meta must agree."
                )

        profile = cls._from_raw(model_key, firmware, raw)

        if profile.status != "VERIFIED":
            logger.warning(
                "[%s @ %s] Profile status is %s — values are not fully verified on real hardware",
                model_key,
                profile.ble_name,
                profile.status,
            )
        for spec in profile.commands.values():
            if spec.provenance is not None and spec.provenance.status != "VERIFIED":
                logger.info(
                    "[%s @ %s] Command '%s' provenance status is %s",
                    model_key,
                    profile.ble_name,
                    spec.name,
                    spec.provenance.status,
                )
        return profile

    @classmethod
    def _from_raw(cls, model_key: str, firmware: str, raw: dict) -> CameraProfile:
        """Build a CameraProfile from an already-parsed profile dict.

        Deliberately lenient — missing sections fall back to defaults so unit
        tests can build partial profiles. Schema validation is `for_model`'s
        job (the load-time boundary), not this constructor's.
        """
        meta = raw.get("_meta", {})
        ble = raw.get("ble", {})
        gap_meta_data = raw.get("gap_meta_data", {})
        device_info_meta_data = raw.get("device_info_meta_data", {})

        commands: dict[str, CommandSpec] = {}
        for name, block in raw.get("commands", {}).items():
            if name.startswith("_"):
                continue
            provenance_raw = block.get("provenance")
            provenance = (
                CommandProvenance(
                    status=provenance_raw.get("status", "UNVERIFIED"),
                    method=provenance_raw.get("method"),
                    capture_refs=tuple(provenance_raw.get("capture_refs", ())),
                    verified_on=provenance_raw.get("verified_on"),
                    notes=provenance_raw.get("notes"),
                )
                if provenance_raw is not None
                else None
            )
            commands[name] = CommandSpec(
                name=name,
                category=block["category"],
                parameter=block["parameter"],
                data_type=DataType[block["data_type"]],
                values={
                    key: value for key, value in block["values"].items() if not key.startswith("_")
                },
                reserved=block.get("reserved", 0x00),
                operation=block.get("operation"),
                echo_operation=block.get("echo_operation"),
                provenance=provenance,
            )

        return cls(
            model_key=model_key,
            model_name=meta.get("model", model_key),
            firmware=firmware,
            status=meta.get("status", "UNKNOWN"),
            ble_name=meta.get("ble_name", ""),
            incoming_uuid=ble.get("characteristic_incoming", CHARACTERISTIC_INCOMING),
            gap_metadata_readable=gap_meta_data.get("readable", False),
            device_info_metadata_readable=device_info_meta_data.get("readable", False),
            commands=commands,
            _raw=raw,
        )


# ── Registry helpers ────────────────────────────────────────────────────────────────────

_profile_cache: dict[tuple[str, str], CameraProfile] = {}


def get_profile(model_key: str, firmware: str) -> CameraProfile:
    """Cached profile loader. Returns same instance for repeated calls."""
    key = (model_key, firmware)
    if key not in _profile_cache:
        _profile_cache[key] = CameraProfile.for_model(model_key, firmware)
    return _profile_cache[key]
