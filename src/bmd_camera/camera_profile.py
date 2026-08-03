"""
bmd_camera/camera_profile.py
=====================
CameraProfile dataclass — the single source of truth for all
model/firmware-specific constants.

Every value that can differ between camera models or firmware versions lives
here and is loaded from payloads/models/<MODEL_KEY>/ble/<FIRMWARE>.json, which
is validated against payloads/ble_schema.json at load time.

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
1. Create payloads/models/<MODEL_KEY>/ble/<FIRMWARE>.json conforming to
   payloads/ble_schema.json (see docs/ble/payload_profiles.md).
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

from .ble.constants import CHARACTERISTIC_INCOMING
from .ble.protocol.types import DataType

logger = logging.getLogger(__name__)

PAYLOADS_DIR = Path(__file__).resolve().parents[2] / "payloads"
MODELS_DIR = PAYLOADS_DIR / "models"
SCHEMA_PATH = PAYLOADS_DIR / "ble_schema.json"
REST_SCHEMA_PATH = PAYLOADS_DIR / "rest_schema.json"

# Registry of all known (model_key, firmware) pairs.
# Add a new tuple here after creating the corresponding JSON file.
KNOWN_PROFILES: list[tuple[str, str]] = [
    ("POCKET_6K_G2", "v7.9"),
    ("POCKET_6K_G2", "v8.6"),
    ("POCKET_6K_PRO", "v8.6"),
]


@lru_cache(maxsize=1)
def load_schema() -> dict:
    """Load and cache payloads/ble_schema.json."""
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def validate_profile(raw: dict, *, source: str) -> None:
    """Validate a raw profile dict against payloads/ble_schema.json.

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


@lru_cache(maxsize=1)
def load_rest_schema() -> dict:
    """Load and cache payloads/rest_schema.json."""
    with open(REST_SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def validate_rest_profile(raw: dict, *, source: str) -> None:
    """Validate a raw REST profile dict against payloads/rest_schema.json.

    Same contract as ``validate_profile``, for the ``rest/<firmware>.json``
    sibling files populated from ``tools/rest/probe_endpoints.py`` sweep
    output.
    """
    try:
        jsonschema.validate(raw, load_rest_schema())
    except jsonschema.ValidationError as exc:
        raise ValueError(
            f"REST profile {source} failed schema validation at {exc.json_path}: {exc.message}"
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
    e.g. ``{"start": 2, "stop": 0}`` for recording. It is empty for
    multi-element families (codec_quality, video_format, recording_format)
    whose payloads are composed from the profile's lookup tables
    (``codecs``/``resolutions``/``fps_modes``) instead.
    """

    name: str
    category: int
    parameter: int
    data_type: DataType
    values: dict[str, int] = field(default_factory=dict)
    reserved: int = 0x00
    operation: int | None = None
    echo_operation: int | None = None
    provenance: CommandProvenance | None = None


@dataclass(frozen=True)
class CodecSpec:
    """One codec family from a profile's ``codecs`` table.

    ``id`` is the codec_quality payload's first element; ``variants`` maps
    quality-variant names to its second element. Variant ids are per-codec —
    BRAW's ``0`` (Q0) and ProRes's ``0`` (HQ) are unrelated values.
    """

    name: str
    id: int
    variants: dict[str, int]


@dataclass(frozen=True)
class ResolutionSpec:
    """One resolution from a profile's ``resolutions`` table.

    ``width``/``height`` feed the recording_format payload; ``codecs`` names
    the codec families the camera offers at this resolution;
    ``dimension_enums`` maps a codec name to the video_format
    dimension_enum byte (the enum encodes resolution AND codec family
    together). A codec present in ``codecs`` but absent from
    ``dimension_enums`` is supported by the camera but its enum has not
    been captured yet. ``known_unreachable`` maps a codec name to an
    evidence note for a combination the camera itself can reach (confirmed
    through its own body menu) but that this codebase's writes cannot reach
    over BLE despite exhausting every write-value hypothesis — a software
    gap, not a camera capability gap (see docs/ble/settings.md).
    ``max_fps_int`` is a different flavor again: the highest
    ``fps_modes.<name>.fps_int`` this resolution supports on the camera
    itself (a genuine hardware/sensor-readout ceiling, e.g.
    ``POCKET_6K_PRO v8.6``'s '6K' topping out at 50 — confirmed absent from
    the camera's own UI at 6K, not just unconfirmed by echo). ``None`` means
    no known ceiling.
    """

    name: str
    width: int
    height: int
    codecs: tuple[str, ...] = ()
    dimension_enums: dict[str, int] = field(default_factory=dict)
    known_unreachable: dict[str, str] = field(default_factory=dict)
    max_fps_int: int | None = None


@dataclass(frozen=True)
class FpsModeSpec:
    """One frame-rate mode from a profile's ``fps_modes`` table.

    ``fps_int`` is the integer frame rate shared by the video_format and
    recording_format payloads; ``m_rate`` is video_format's flag (0 exact,
    1 NTSC/drop); ``frame_flags`` is recording_format's int16 flags value.
    """

    name: str
    fps_int: int
    m_rate: int
    frame_flags: int


@dataclass(frozen=True)
class StorageSignalSpec:
    """One sniffer-observed passive storage-monitoring signal from a
    profile's ``storage`` map.

    Unlike ``CommandSpec``, nothing here is ever sent by this repo's code —
    these are decode-only hints for notifications the camera emits
    unprompted (CLAUDE.md design principle 10, planned storage monitoring).
    That's why this doesn't carry ``reserved``/``operation``/
    ``echo_operation``: those only mean something for a command this repo
    encodes and writes to ``OUTGOING_CONTROL``.
    """

    name: str
    category: int
    parameter: int
    data_type: DataType
    values: dict[str, int]
    byte_offset: int = 0
    provenance: CommandProvenance | None = None


@dataclass(frozen=True)
class RestEndpointSpec:
    """One REST endpoint's sweep outcome from a profile's ``endpoints`` map.

    ``status``/``supported`` come from the read sweep; ``put_status``/
    ``put_supported`` are only meaningful when ``tools/rest/probe_endpoints.py
    --probe-writes`` ran for this path. A same-value PUT proves the endpoint
    exists, not that a *changing* write applies — see docs/rest/transport.md.
    """

    path: str
    status: int | None
    supported: bool
    notes: str | None = None
    put_status: int | None = None
    put_supported: bool | None = None


@dataclass(frozen=True)
class RestProfile:
    """Resolved REST/WebSocket profile for one (model_key, firmware) pair,
    loaded from payloads/models/<MODEL_KEY>/rest/<FIRMWARE>.json when that
    file exists.

    Absent entirely for a camera/firmware with no REST sweep yet —
    ``CameraProfile.rest`` is then this class's all-defaults instance,
    mirroring a Phase 1 BLE scaffold's ``commands == {}``: the profile still
    loads, there's just nothing here to read. ``RestCameraSession`` refuses
    to call a known-501 endpoint rather than rediscovering it every run —
    the role ``known_unreachable`` plays for BLE (see docs/rest/transport.md).
    """

    status: str = "UNKNOWN"
    transport: str | None = None
    endpoints: dict[str, RestEndpointSpec] = field(default_factory=dict)
    websocket_properties: tuple[str, ...] = ()
    format_names: dict[str, dict[str, str]] = field(default_factory=dict)
    provenance: CommandProvenance | None = None


def _parse_provenance(block: dict) -> CommandProvenance | None:
    """Parse a ``provenance`` sub-block shared by ``commands`` and ``storage`` entries."""
    provenance_raw = block.get("provenance")
    if provenance_raw is None:
        return None
    return CommandProvenance(
        status=provenance_raw.get("status", "UNVERIFIED"),
        method=provenance_raw.get("method"),
        capture_refs=tuple(provenance_raw.get("capture_refs", ())),
        verified_on=provenance_raw.get("verified_on"),
        notes=provenance_raw.get("notes"),
    )


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

    # Passively-observed storage-monitoring signals, keyed by signal name.
    # Never sent — see StorageSignalSpec's docstring.
    storage: dict[str, StorageSignalSpec] = field(default_factory=dict)

    # Settings lookup tables consumed by the codec_quality / video_format /
    # recording_format command families (docs/ble/settings.md). Empty until
    # sniffed on the camera — their provenance rides with the command
    # blocks that consume them.
    codecs: dict[str, CodecSpec] = field(default_factory=dict)
    resolutions: dict[str, ResolutionSpec] = field(default_factory=dict)
    fps_modes: dict[str, FpsModeSpec] = field(default_factory=dict)

    # REST/WebSocket profile (Phase 2+), loaded from a sibling rest/<fw>.json
    # when present. Defaults to an all-empty RestProfile — see its docstring.
    rest: RestProfile = field(default_factory=RestProfile)

    # Raw JSON for reference
    _raw: dict = field(default_factory=dict, repr=False, compare=False)

    # ── Command access ───────────────────────────────────────────────────────

    def command(self, name: str) -> CommandSpec | None:
        """The named command block, or None when this profile lacks it."""
        return self.commands.get(name)

    def require_command(self, name: str, value_names: tuple[str, ...] = ()) -> CommandSpec:
        """Return the named command block, raising ValueError when the block
        or any of ``value_names`` is missing from the profile JSON."""
        json_path = self._json_path()
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

    # ── REST endpoint access ────────────────────────────────────────────────

    def rest_endpoint(self, path: str) -> RestEndpointSpec | None:
        """The named REST endpoint's sweep outcome, or None when this
        profile has no ``rest/`` file, or the file has no entry for
        ``path``."""
        return self.rest.endpoints.get(path)

    def require_rest_endpoint(self, path: str) -> RestEndpointSpec:
        """Return the named REST endpoint's sweep outcome, raising
        ValueError when it is missing from the profile's ``rest/`` file (or
        no such file exists yet)."""
        spec = self.rest.endpoints.get(path)
        if spec is None:
            known = ", ".join(sorted(self.rest.endpoints)) or "(none)"
            raise ValueError(
                f"Profile {self.model_key}_{self.firmware} has no REST endpoint '{path}' "
                f"(known: {known}) — run tools/rest/probe_endpoints.py against this "
                f"camera/firmware first and paste the result into "
                f"payloads/models/{self.model_key}/rest/{self.firmware}.json."
            )
        return spec

    # ── Storage signal access ───────────────────────────────────────────────

    def storage_signal(self, name: str) -> StorageSignalSpec | None:
        """The named storage signal block, or None when this profile lacks it."""
        return self.storage.get(name)

    def require_storage_signal(
        self, name: str, value_names: tuple[str, ...] = ()
    ) -> StorageSignalSpec:
        """Return the named storage signal block, raising ValueError when the
        block or any of ``value_names`` is missing from the profile JSON."""
        json_path = self._json_path()
        spec = self.storage.get(name)
        if spec is None:
            raise ValueError(
                f"Profile {self.model_key}_{self.firmware} has no '{name}' storage block — "
                f"populate {json_path}'s storage.{name} first."
            )
        missing = [value for value in value_names if value not in spec.values]
        if missing:
            raise ValueError(
                f"Profile {self.model_key}_{self.firmware} storage signal '{name}' is missing "
                f"values: {', '.join(missing)} — populate storage.{name}.values in {json_path}."
            )
        return spec

    # ── Settings lookup-table access ────────────────────────────────────────

    def _json_path(self) -> str:
        return f"payloads/models/{self.model_key}/ble/{self.firmware}.json"

    def require_codec(self, name: str, variant: str | None = None) -> CodecSpec:
        """Return the named codec table entry, raising ValueError when the
        codec (or the requested ``variant``) is missing from the profile JSON."""
        spec = self.codecs.get(name)
        if spec is None:
            known = ", ".join(sorted(self.codecs)) or "(none)"
            raise ValueError(
                f"Profile {self.model_key}_{self.firmware} has no codec '{name}' "
                f"(known: {known}) — populate {self._json_path()}'s codecs table from a capture."
            )
        if variant is not None and variant not in spec.variants:
            known = ", ".join(sorted(spec.variants)) or "(none)"
            raise ValueError(
                f"Profile {self.model_key}_{self.firmware} codec '{name}' has no variant "
                f"'{variant}' (known: {known}) — populate codecs.{name}.variants in "
                f"{self._json_path()}."
            )
        return spec

    def require_resolution(self, name: str) -> ResolutionSpec:
        """Return the named resolution table entry, raising ValueError when
        it is missing from the profile JSON."""
        spec = self.resolutions.get(name)
        if spec is None:
            known = ", ".join(sorted(self.resolutions)) or "(none)"
            raise ValueError(
                f"Profile {self.model_key}_{self.firmware} has no resolution '{name}' "
                f"(known: {known}) — populate {self._json_path()}'s resolutions table "
                f"from a capture."
            )
        return spec

    def require_fps_mode(self, name: str) -> FpsModeSpec:
        """Return the named fps-mode table entry, raising ValueError when it
        is missing from the profile JSON."""
        spec = self.fps_modes.get(name)
        if spec is None:
            known = ", ".join(sorted(self.fps_modes)) or "(none)"
            raise ValueError(
                f"Profile {self.model_key}_{self.firmware} has no fps mode '{name}' "
                f"(known: {known}) — populate {self._json_path()}'s fps_modes table "
                f"from a capture."
            )
        return spec

    # ── Construction ─────────────────────────────────────────────────────────

    @classmethod
    def for_model(cls, model_key: str, firmware: str) -> CameraProfile:
        """
        Load and return a CameraProfile for (model_key, firmware).

        Searches payloads/models/<MODEL_KEY>/ble/<FIRMWARE>.json, validates it
        against payloads/ble_schema.json, and cross-checks the file's _meta
        identity against the requested pair — _meta.model_key against the
        grandparent directory, _meta.firmware against the file stem. Raises
        FileNotFoundError if the JSON does not exist and ValueError on
        validation failure. Logs a prominent warning when the profile is not
        VERIFIED (CLAUDE.md design principle 8).
        """
        filename = MODELS_DIR / model_key / "ble" / f"{firmware}.json"
        relative_path = f"payloads/models/{model_key}/ble/{firmware}.json"
        if not filename.exists():
            raise FileNotFoundError(
                f"No model JSON found at {relative_path}. Create it conforming to "
                "payloads/ble_schema.json."
            )
        with open(filename, encoding="utf-8") as fh:
            raw = json.load(fh)

        validate_profile(raw, source=relative_path)

        meta = raw.get("_meta", {})
        for key, expected in (("model_key", model_key), ("firmware", firmware)):
            actual = meta.get(key)
            if actual != expected:
                raise ValueError(
                    f"Profile {relative_path} declares _meta.{key}={actual!r} "
                    f"but was loaded as {expected!r} — filename and _meta must agree."
                )

        profile = cls._from_raw(model_key, firmware, raw)
        profile.rest = cls._load_rest_profile(model_key, firmware)

        if profile.status != "VERIFIED":
            logger.warning(
                "[%s @ %s] Profile status is %s — values are not fully verified on real hardware",
                model_key,
                profile.ble_name,
                profile.status,
            )
        for spec in (*profile.commands.values(), *profile.storage.values()):
            if spec.provenance is not None and spec.provenance.status != "VERIFIED":
                logger.info(
                    "[%s @ %s] '%s' provenance status is %s",
                    model_key,
                    profile.ble_name,
                    spec.name,
                    spec.provenance.status,
                )
        if profile.rest.endpoints and profile.rest.status != "VERIFIED":
            logger.info(
                "[%s @ %s] REST profile status is %s — endpoints are sweep-derived, "
                "not confirmed by a changing write",
                model_key,
                profile.ble_name,
                profile.rest.status,
            )
        return profile

    @classmethod
    def _load_rest_profile(cls, model_key: str, firmware: str) -> RestProfile:
        """Load payloads/models/<MODEL_KEY>/rest/<FIRMWARE>.json if it
        exists, else return an all-defaults RestProfile.

        A REST file is optional per camera (Phase 2 profile plumbing) — its
        absence is not an error, unlike a missing BLE profile. When present,
        it is validated and its ``_meta`` cross-checked exactly like the BLE
        file's.
        """
        filename = MODELS_DIR / model_key / "rest" / f"{firmware}.json"
        if not filename.exists():
            return RestProfile()

        relative_path = f"payloads/models/{model_key}/rest/{firmware}.json"
        with open(filename, encoding="utf-8") as fh:
            raw = json.load(fh)

        validate_rest_profile(raw, source=relative_path)

        meta = raw.get("_meta", {})
        for key, expected in (("model_key", model_key), ("firmware", firmware)):
            actual = meta.get(key)
            if actual != expected:
                raise ValueError(
                    f"REST profile {relative_path} declares _meta.{key}={actual!r} "
                    f"but was loaded as {expected!r} — filename and _meta must agree."
                )

        return cls._rest_from_raw(raw)

    @staticmethod
    def _rest_from_raw(raw: dict) -> RestProfile:
        """Build a RestProfile from an already-parsed rest/<fw>.json dict."""
        meta = raw.get("_meta", {})

        endpoints: dict[str, RestEndpointSpec] = {}
        for path, block in raw.get("endpoints", {}).items():
            if path.startswith("_"):
                continue
            endpoints[path] = RestEndpointSpec(
                path=path,
                status=block.get("status"),
                supported=block.get("supported", False),
                notes=block.get("notes"),
                put_status=block.get("put_status"),
                put_supported=block.get("put_supported"),
            )

        format_names: dict[str, dict[str, str]] = {}
        for family, variants in raw.get("format_names", {}).items():
            if family.startswith("_"):
                continue
            format_names[family] = {
                variant: rest_name
                for variant, rest_name in variants.items()
                if not variant.startswith("_")
            }

        return RestProfile(
            status=meta.get("status", "UNKNOWN"),
            transport=raw.get("transport"),
            endpoints=endpoints,
            websocket_properties=tuple(raw.get("websocket_properties", ())),
            format_names=format_names,
            provenance=_parse_provenance(raw),
        )

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
            commands[name] = CommandSpec(
                name=name,
                category=block["category"],
                parameter=block["parameter"],
                data_type=DataType[block["data_type"]],
                values={
                    key: value
                    for key, value in block.get("values", {}).items()
                    if not key.startswith("_")
                },
                reserved=block.get("reserved", 0x00),
                operation=block.get("operation"),
                echo_operation=block.get("echo_operation"),
                provenance=_parse_provenance(block),
            )

        storage: dict[str, StorageSignalSpec] = {}
        for name, block in raw.get("storage", {}).items():
            if name.startswith("_"):
                continue
            storage[name] = StorageSignalSpec(
                name=name,
                category=block["category"],
                parameter=block["parameter"],
                data_type=DataType[block["data_type"]],
                values={
                    key: value for key, value in block["values"].items() if not key.startswith("_")
                },
                byte_offset=block.get("byte_offset", 0),
                provenance=_parse_provenance(block),
            )

        codecs: dict[str, CodecSpec] = {}
        for name, block in raw.get("codecs", {}).items():
            if name.startswith("_"):
                continue
            codecs[name] = CodecSpec(
                name=name,
                id=block["id"],
                variants={
                    key: value
                    for key, value in block["variants"].items()
                    if not key.startswith("_")
                },
            )

        resolutions: dict[str, ResolutionSpec] = {}
        for name, block in raw.get("resolutions", {}).items():
            if name.startswith("_"):
                continue
            resolutions[name] = ResolutionSpec(
                name=name,
                width=block["width"],
                height=block["height"],
                codecs=tuple(block.get("codecs", ())),
                dimension_enums={
                    key: value
                    for key, value in block.get("dimension_enums", {}).items()
                    if not key.startswith("_")
                },
                known_unreachable={
                    key: value
                    for key, value in block.get("known_unreachable", {}).items()
                    if not key.startswith("_")
                },
                max_fps_int=block.get("max_fps_int"),
            )

        fps_modes: dict[str, FpsModeSpec] = {}
        for name, block in raw.get("fps_modes", {}).items():
            if name.startswith("_"):
                continue
            fps_modes[name] = FpsModeSpec(
                name=name,
                fps_int=block["fps_int"],
                m_rate=block["m_rate"],
                frame_flags=block["frame_flags"],
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
            storage=storage,
            codecs=codecs,
            resolutions=resolutions,
            fps_modes=fps_modes,
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
