# Payload Profiles — structure, schema, and provenance

**Status:** implemented — schema-validated profiles + `CommandSpec` are live. `storage` has its first entry (a CANDIDATE signal, `StorageSignalSpec`); the settings lookup tables (`codecs`/`resolutions`/`fps_modes`) have their first population on `POCKET_6K_G2 v7.9` (CANDIDATE — see `docs/settings.md`); `capabilities` remains reserved and unpopulated.

## Overview

`payloads/models/<MODEL_KEY>_<FIRMWARE>.json` is the single source of truth
for every model/firmware-specific protocol value (CLAUDE.md design
principles 1 and 2). This doc records the profile structure, the JSON Schema
that enforces it (`payloads/schema.json`), how `camera_profile.py` loads and
validates it, and — since the structure was a design decision — the
alternatives that were considered and why this shape won.

---

## Structures considered

The original layout had one bespoke top-level block per feature
(`recording.start_value` / `recording.stop_value`), plus free-text
`_comment` strings carrying verification history. Candidate replacements:

**A. Flat per-feature blocks (status quo, extended)** — a new hand-shaped
top-level block per command family (`recording`, `photo`, `playback`, ...),
each with its own field names (`start_value`, `trigger_value`, ...).
Rejected: every new command family needs new `CameraProfile` dataclass
fields, new `require_*` helpers, and schema additions; nothing generic can
consume it (the discovery tool would need per-family output code); and
verification history stays informal.

**B. Raw packet library** — store complete captured packets as hex strings
(`"record_start": "FF 05 00 01 0A 01 01 00 02"`) and replay them.
Rejected: opaque (no way to check a category against the spec, adjust a
payload width, or reason about echoes), duplicates the header per value, and
makes decode/verify logic impossible to share.

**C. Generic `commands` map with named `values` and per-block `provenance`
(chosen)** — every command family is the *same shape*: protocol coordinates
(`category`, `parameter`, `data_type`, `reserved`), a `values` map naming
each payload integer, the observed `echo_operation`, and structured
`provenance`. One `CommandSpec` dataclass serves all families; the discovery
tool emits blocks directly; JSON Schema can validate every block with one
`$defs` entry.

Lookup tables and `capabilities` are *not* commands — they are data tables
consumed by settings code — so they stay separate top-level sections,
absent from a profile until sniffed on that camera (principle 6: never
invent values ahead of captures).

The lookup tables were originally reserved as four flat maps (`codec_ids`,
`quality_ids`, `resolution_ids`, `fps_encodings`). When the first real
settings values arrived (the `POCKET_6K_G2 v7.9` external-RE transcription,
`docs/settings.md`), flat maps turned out to misrepresent the data in three
ways: quality-variant ids are **per-codec** (BRAW's 0 = Q0, ProRes's 0 =
HQ — one flat `quality_ids` map cannot hold both), the video_format
dimension enum is keyed by **(resolution, codec)** — not resolution alone —
because it encodes the codec family too, and an FPS mode needs **three**
values (`fps_int`, `m_rate`, `frame_flags`), not a two-int tuple. Since
nothing had ever populated the old sections, they were replaced (not
deprecated) with three structured sections:

- **`codecs`** — codec name → `{id, variants: {name: id}}` (`CodecSpec`)
- **`resolutions`** — label → `{width, height, codecs: [...],
  dimension_enums: {codec: enum}}` (`ResolutionSpec`); a codec listed in
  `codecs` but missing from `dimension_enums` means "supported, enum not
  captured yet"
- **`fps_modes`** — label → `{fps_int, m_rate, frame_flags}` (`FpsModeSpec`)

The tables carry no `provenance` block of their own: their provenance rides
with the command blocks that consume them (`codec_quality`,
`video_format`, `recording_format`), whose notes name the source captures
or documents. Relatedly, a command block's `values` map is now **optional**:
those three multi-element families compose their payloads from the lookup
tables, so a named-scalar `values` map has nothing to say for them
(`CommandSpec.values` defaults to `{}`; `require_command` with
`value_names` still fails loudly on a family that *should* have values).

`storage` is no longer purely reserved: the first CANDIDATE storage signal
(a write-margin warning correlated with camera-initiated recording stops on
a slow SD card — see `docs/recording.md`'s "Camera-initiated stop
detection") has landed in both real profiles. `storage` entries are the
*same generic shape* as `commands` — protocol coordinates, a named `values`
map, structured `provenance` — minus the send-only fields (`reserved`,
`operation`, `echo_operation`), since nothing under `storage` is ever
encoded and sent by this repo's code; these are decode-only hints for
notifications the camera emits unprompted. A sibling `StorageSignalSpec`
dataclass (not `CommandSpec`) models this in `camera_profile.py`, and
`payloads/schema.json` validates it via a `$defs/storageSignal` entry
mirroring `$defs/command`.

---

## The chosen structure

```json
{
  "_meta": {
    "model": "Pocket Cinema Camera 6K G2",
    "model_key": "POCKET_6K_G2",
    "firmware": "v7.9",
    "ble_name": "A:AF3DC814",
    "status": "UNVERIFIED"
  },
  "ble": { "incoming_property": "indicate" },
  "gap_meta_data": { "readable": false },
  "device_info_meta_data": { "readable": false },
  "commands": {
    "recording": {
      "category": 10,
      "parameter": 1,
      "data_type": "INT8",
      "reserved": 1,
      "values": { "start": 2, "stop": 0 },
      "echo_operation": 2,
      "provenance": {
        "status": "VERIFIED",
        "method": "passive sniffer + active send-and-capture + echo-verified CameraSession",
        "capture_refs": ["tools/captures/POCKET_6K_G2_v7.9/ (local, gitignored)"],
        "verified_on": "2026-06-19",
        "notes": "..."
      }
    }
  }
}
```

### Design decisions

- **`values` is a `{name: int}` map**, not `start_value`/`stop_value`
  fields. Names are what code addresses (`spec.values["start"]`) and what
  the discovery tool's `--outcomes` flag produces; integers are the
  sniffer-captured payload bytes. Any command family fits without schema or
  dataclass changes — photo capture might be `{"trigger": 1}`, playback
  `{"play": 1, "pause": 0, "next": 2}`.
- **`data_type` is a symbolic string** matching `protocol/types.py`
  `DataType` names (official spec coding — see `docs/protocol.md` §3) —
  human-readable in captures and diffs, and validated by the schema's
  shared `$defs/dataType` enum. `"BOOL"` is accepted as an alias of wire
  code 0 (`VOID`, the spec's "void/boolean"); `"INT16_ARRAY"` is the one
  non-official name (CANDIDATE wire byte `0x82`, `docs/settings.md` §3).
- **`echo_operation` is a raw integer** (0–255), not a symbolic `Operation`
  name: it is copied verbatim from captures, and a future camera may report
  an operation byte not yet in the enum — an int survives that.
- **Command `operation` is optional** and absent everywhere today: every
  controller-issued command observed so far is `ASSIGN` (0x00). The schema
  reserves the field so a future `OFFSET`-style command doesn't force a
  schema change.
- **`reserved` is per-block and recorded from the capture.** The codec's
  generic default is `0x00`, but the G2's real recording command uses
  `0x01` — always store what was actually captured.
- **Per-block `provenance` is required.** `_meta.status` describes the
  whole profile (design principle 8) and stays `UNVERIFIED` until *every*
  populated section is hardware-tested; `provenance.status` tracks each
  command family individually (`VERIFIED` / `CANDIDATE` / `UNVERIFIED`).
  This replaces the free-text `_comment` history with structured data:
  method, capture file references, verification date, notes.
- **`_comment` keys are allowed anywhere** (schema `patternProperties:
  "^_"`) and skipped by the loader.

### A `storage` entry

```json
"storage": {
  "write_margin_warning": {
    "category": 9,
    "parameter": 1,
    "data_type": "INT8",
    "byte_offset": 1,
    "values": { "nominal": 1, "low_margin": -2 },
    "provenance": {
      "status": "CANDIDATE",
      "method": "passive observation from examples/record_start_stop.py real-hardware logs during induced SD card slow-write-speed testing",
      "notes": "..."
    }
  }
}
```

- **`byte_offset`** (new, `storage`-only field, defaults to `0`) — the byte
  offset within the payload that carries the signal. Every `commands` entry
  so far reads from the start of the payload (offset 0, implicit); this
  write-margin signal's meaningful byte was observed at offset 1 instead —
  offsets 0 and 2 are constant and unexplained on every capture seen so
  far. Rather than assume offset 0 is universal, `byte_offset` makes it an
  explicit, sniffer-observed, profile-supplied fact (CLAUDE.md design
  principle 1) instead of a Python default.
- **No `reserved`/`operation`/`echo_operation`** — these only mean something
  for a block this repo encodes and writes to `OUTGOING_CONTROL`; a
  `storage` entry is decode-only.
- **`values` only records exact observed values**, e.g. `-2` for
  `low_margin` — never a range or sign-based generalization. Code compares
  a decoded reading against `spec.values["low_margin"]` explicitly, not
  "any negative value" (CLAUDE.md design principle 6).

---

## Schema validation at load time

`payloads/schema.json` (JSON Schema draft 2020-12) validates every profile
in `CameraProfile.for_model` via the `jsonschema` package (the one runtime
dependency besides `bleak`). Key properties:

- `_meta` and every command block set `additionalProperties: false`, so a
  typo like `start_val` fails loudly at load time instead of silently
  falling back to a default.
- `validate_profile(raw, source=...)` wraps `jsonschema.ValidationError` in
  a `ValueError` naming the offending JSON path and source file.
- `for_model` also cross-checks `_meta.model_key`/`_meta.firmware` against
  the requested pair (the filename) — the schema cannot express that.
- **`_from_raw` stays deliberately lenient** (missing sections fall back to
  defaults). Validation is `for_model`'s job — the load-time boundary
  CLAUDE.md promises — and the leniency is what lets unit tests build
  partial profiles. Do not "fix" this by validating in `_from_raw`.

Loading an `UNVERIFIED` profile logs a prominent warning (design principle
8); each command block whose provenance is not `VERIFIED` logs at `INFO`.

---

## Code surface (`src/bmd_ble/camera_profile.py`)

```python
profile = CameraProfile.for_model("POCKET_6K_G2", "v7.9")   # validated
spec = profile.require_command("recording", ("start", "stop"))  # CommandSpec

codec = profile.require_codec("BRAW", "5:1")     # CodecSpec
codec.id                          # 3
codec.variants["5:1"]             # 3
resolution = profile.require_resolution("4K DCI")  # ResolutionSpec
resolution.width                  # 4096
resolution.dimension_enums        # {"BRAW": 8} — ProRes enum not captured yet
fps = profile.require_fps_mode("23.98")            # FpsModeSpec
(fps.fps_int, fps.m_rate, fps.frame_flags)         # (24, 1, 19)
spec.category            # 10
spec.data_type           # DataType.INT8
spec.values["start"]     # 2
spec.reserved            # 1
spec.echo_operation      # 2
spec.provenance.status   # "VERIFIED"

storage_spec = profile.require_storage_signal(
    "write_margin_warning", ("nominal", "low_margin")
)  # StorageSignalSpec
storage_spec.category            # 9
storage_spec.byte_offset         # 1
storage_spec.values["low_margin"]  # -2
storage_spec.provenance.status   # "CANDIDATE"
```

- `CommandSpec` (frozen dataclass) carries one command family;
  `CommandProvenance` carries its provenance.
- `profile.command(name)` returns `None` when absent (for capability-style
  checks); `profile.require_command(name, value_names)` raises a
  `ValueError` that names the missing block or value names and points at the
  profile JSON path — both `session.py` and `tools/control/` fail the same
  way on an unpopulated profile.
- `StorageSignalSpec` (frozen dataclass) carries one passively-decoded
  storage signal, sharing `CommandProvenance` but not `CommandSpec` (see "A
  `storage` entry" above for why). `profile.storage_signal(name)` /
  `profile.require_storage_signal(name, value_names)` mirror
  `command`/`require_command` exactly, pointing at `storage.<name>` in the
  profile JSON path.

- `require_codec(name, variant=None)` / `require_resolution(name)` /
  `require_fps_mode(name)` mirror `require_command`'s fail-loud contract
  for the settings lookup tables, additionally listing the names the
  profile *does* know — an operator typo ("5.7K" vs "5.7K 17:9") surfaces
  the valid labels immediately.

Consumers: `session.py` (`record_start`/`record_stop`,
`_observe_write_margin`, and the settings writes `set_codec_quality`/
`set_video_format`/`set_recording_format`),
`tools/control/send_record_command.py`,
`tools/control/send_settings_command.py`, and
`tools/control/discover_command.py` (emits new `commands` blocks in exactly
this shape — see `docs/command_discovery.md`).

---

## Adding a new command block

1. Capture the command on the target camera/firmware
   (`tools/sniffers/` passively, or `tools/control/discover_command.py` for
   the guided send-and-confirm path).
2. Paste the emitted block under `commands` in the profile JSON.
3. Run `python -m pytest tests/unit/` — `test_camera_profile.py`
   parametrizes over `KNOWN_PROFILES` and validates every real profile
   against the schema, so a malformed block fails CI immediately.

## Testing

`tests/unit/test_camera_profile.py` covers: every `KNOWN_PROFILES` JSON
validating and loading (including both profiles' `storage.write_margin_warning`
block resolving identically), `CommandSpec`/`StorageSignalSpec`/provenance
resolution, `require_command`/`require_storage_signal` error messages,
`validate_profile` rejections (typos, bad enums, wrong value types, missing
provenance — for both `commands` and `storage` blocks), the filename↔`_meta`
cross-check, the UNVERIFIED load warning (and the non-VERIFIED provenance
INFO log now covering `storage` too), and `_from_raw` leniency.
