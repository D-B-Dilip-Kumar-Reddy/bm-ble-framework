# Payload Profiles — structure, schema, and provenance

**Status:** implemented — schema-validated profiles + `CommandSpec` are live; capability/lookup sections are reserved, not yet populated.

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

Lookup tables (`codec_ids`, `quality_ids`, `resolution_ids`,
`fps_encodings`), `capabilities`, and `storage` are *not* commands — they are
data tables consumed by future settings/storage code — so they stay separate
top-level sections. They are defined in the schema now but deliberately
absent from the profiles until sniffed on each camera (principle 6: never
invent values ahead of captures).

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
  human-readable in captures and diffs, and validated by the schema's enum.
  `"BOOL"` is accepted as an alias of wire code 0 (`VOID`, the spec's
  "void/boolean").
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
spec.category            # 10
spec.data_type           # DataType.INT8
spec.values["start"]     # 2
spec.reserved            # 1
spec.echo_operation      # 2
spec.provenance.status   # "VERIFIED"
```

- `CommandSpec` (frozen dataclass) carries one command family;
  `CommandProvenance` carries its provenance.
- `profile.command(name)` returns `None` when absent (for capability-style
  checks); `profile.require_command(name, value_names)` raises a
  `ValueError` that names the missing block or value names and points at the
  profile JSON path — both `session.py` and `tools/control/` fail the same
  way on an unpopulated profile.

Consumers: `session.py` (`record_start`/`record_stop`),
`tools/control/send_record_command.py`, and
`tools/control/discover_command.py` (emits new blocks in exactly this
shape — see `docs/command_discovery.md`).

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
validating and loading, `CommandSpec`/provenance resolution, `require_command`
error messages, `validate_profile` rejections (typos, bad enums, wrong value
types, missing provenance), the filename↔`_meta` cross-check, the UNVERIFIED
load warning, and `_from_raw` leniency.
