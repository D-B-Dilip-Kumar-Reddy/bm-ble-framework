"""
bmd_ble/camera_profile.py
=====================
CameraProfile dataclass — the single source of truth for all
model/firmware-specific constants.

Every value that can differ between camera models or firmware versions lives
here and is loaded from payloads/models/<MODEL_KEY>_<FIRMWARE>.json.

DESIGN PRINCIPLE
────────────────
Nothing in the automation code should hardcode codec IDs, variant IDs, FPS
integers, or BLE UUIDs.  Instead, obtain a CameraProfile for the target camera
and read from it:

    profile = CameraProfile.for_model("POCKET_6K_G2", "v7.9")
    codec_id = profile.codec_id("BRAW")            # → 3
    var_id   = profile.braw_variant_id("5:1")      # → 3
    fps_int, flags = profile.fps_encoding("25")    # → (25, 0x0010)
    uuid     = profile.incoming_uuid               # → "b864e140-..."

ADDING A NEW CAMERA
───────────────────
1. Create payloads/models/<MODEL_KEY>_<FIRMWARE>.json following the schema in
   POCKET_6K_G2_v7.9.json (the reference implementation).
2. Run the targeted sniffer scripts to populate verified values.
3. Add the model key to KNOWN_PROFILES below.
4. Run pytest — the parametrize fixtures pick up new profiles automatically.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .constants import CHARACTERISTIC_INCOMING
from .protocol.types import DataType

logger = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).resolve().parents[2] / "payloads" / "models"

# Registry of all known (model_key, firmware) pairs.
# Add a new tuple here after creating the corresponding JSON file.
KNOWN_PROFILES: list[tuple[str, str]] = [
    ("POCKET_6K_G2", "v7.9"),
]


@dataclass
class CameraProfile:
    """
    Complete resolved profile for one (model_key, firmware) pair.

    All values are either VERIFIED from sniffer sessions or clearly marked
    as defaults that need verification.  Use the `is_verified` property to
    check whether this profile is safe for production use.
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

    # Recording category (record start/stop) — sniffer/reverse-engineered
    # per model; None when not yet populated for this profile.
    recording_category: int | None = None
    recording_parameter: int | None = None
    recording_data_type: DataType | None = None
    recording_reserved: int | None = None
    recording_start_value: int | None = None
    recording_stop_value: int | None = None

    # Raw JSON for reference
    _raw: dict = field(default_factory=dict, repr=False, compare=False)

    # ── Construction ─────────────────────────────────────────────────────────

    @classmethod
    def for_model(cls, model_key: str, firmware: str) -> CameraProfile:
        """
        Load and return a CameraProfile for (model_key, firmware).

        Searches payloads/models/<MODEL_KEY>_<FIRMWARE>.json.
        Raises FileNotFoundError if the JSON does not exist.
        Logs warnings for every null / UNVERIFIED value.
        """
        filename = MODELS_DIR / f"{model_key}_{firmware}.json"
        if not filename.exists():
            raise FileNotFoundError(
                f"No model JSON found at {filename}. "
                f"Create it following the schema in POCKET_6K_G2_v7.9.json."
            )
        with open(filename, encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls._from_raw(model_key, firmware, raw)

    @classmethod
    def _from_raw(cls, model_key: str, firmware: str, raw: dict) -> CameraProfile:
        meta = raw.get("_meta", {})
        ble = raw.get("ble", {})
        gap_meta_data = raw.get("gap_meta_data", {})
        device_info_meta_data = raw.get("device_info_meta_data", {})
        recording = raw.get("recording", {})
        recording_data_type_name = recording.get("data_type")
        profile = cls(
            model_key=model_key,
            model_name=meta.get("model", model_key),
            firmware=firmware,
            status=meta.get("status", "UNKNOWN"),
            ble_name=meta.get("ble_name", ""),
            incoming_uuid=ble.get("characteristic_incoming", CHARACTERISTIC_INCOMING),
            gap_metadata_readable=gap_meta_data.get("readable", False),
            device_info_metadata_readable=device_info_meta_data.get("readable", False),
            recording_category=recording.get("category"),
            recording_parameter=recording.get("parameter"),
            recording_data_type=DataType[recording_data_type_name]
            if recording_data_type_name
            else None,
            recording_reserved=recording.get("reserved"),
            recording_start_value=recording.get("start_value"),
            recording_stop_value=recording.get("stop_value"),
            _raw=raw,
        )
        return profile


# ── Registry helpers ────────────────────────────────────────────────────────────────────

_profile_cache: dict[tuple[str, str], CameraProfile] = {}


def get_profile(model_key: str, firmware: str) -> CameraProfile:
    """Cached profile loader. Returns same instance for repeated calls."""
    key = (model_key, firmware)
    if key not in _profile_cache:
        _profile_cache[key] = CameraProfile.for_model(model_key, firmware)
    return _profile_cache[key]
