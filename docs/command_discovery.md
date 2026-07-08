# Guided Command Discovery

## Overview

`tools/control/discover_command.py` closes the gap between the existing
tools: `tools/sniffers/` can only *observe* what a camera reports, and
`tools/control/send_record_command.py` can only *replay* a command already
fully specified in the profile. Neither can answer the actual
reverse-engineering question for a new camera: **what exact command bytes
does this camera accept for this action?** — the unknown parts being the
payload value(s) and the reserved byte (and, in principle, the operation
byte), even once a passive capture has revealed the (category, parameter,
data_type) coordinates.

The discovery tool answers it with an operator-in-the-loop sweep:

```
A. Seed      (category, parameter, data_type) — from a saved passive capture
             (--from-capture) or CLI flags; docs/protocol.md's [spec] tables
             are the map for choosing seeds
B. Generate  candidate packets: --values × --reserved sweep, ASSIGN operation
C. Probe     send each candidate, capture the response for --listen-seconds,
             show the decoded packets, ask the operator what the camera DID
D. Emit      save the capture as evidence; print a ready-to-paste
             payloads/schema.json-shaped "commands" block
```

It is model- and command-agnostic: nothing recording-specific is hardcoded.
`--label` names the emitted block, `--outcomes` names the confirmable
results — the same tool sweeps recording on a Pocket 6K Pro today and, say,
still-capture on an URSA later.

---

## Module split: pure logic vs interactive driver

| Module | Role | Tested |
|---|---|---|
| `tools/common/discovery.py` | All pure logic — candidate generation, capture-seeding, echo extraction, block building/rendering. No BLE, no `input()`, no filesystem | `tests/unit/tools/common/test_discovery.py` |
| `tools/control/discover_command.py` | Interactive driver — prompts, BLE session, sweep loop | Manual (matches `docs/sniffer_capture_engine.md`'s stance on interactive tools) |
| `protocol/codec.py` `encode_assign` | Category-agnostic ASSIGN-packet encoder the candidates use (also now the body of `recording.py`'s encoder) | `tests/unit/protocol/test_codec.py` |

`tools/common/discovery.py` key functions:

- `generate_candidates(category, parameter, data_type, values, reserveds)`
  — deterministic sweep, operator ordering preserved (put the most likely
  value first so the camera spends the least time in odd states).
- `seed_triples_from_capture(capture_dict, exclude_ambient=True)` — parses
  a saved `tools/common/capture.py` JSON and returns the INCOMING_CONTROL
  `(category, parameter, data_type)` triples. With `exclude_ambient`,
  triples present in *every* window are dropped — ambient telemetry (e.g.
  categories `0x09`/`0x0C` ticking ~1/s on the G2) appears everywhere,
  while the interesting triple appears only in the window where the
  operator triggered the action. **Capture at least two windows** (e.g.
  `record_start` and `record_stop`) or the filter has no contrast and
  keeps everything.
- `extract_echo(notifications, category, parameter)` — first
  cleanly-decoded matching echo as `(operation_int, payload_hex)`.
- `build_command_block(name, confirmed, capture_ref, discovered_on)` —
  assembles the profile block; raises if confirmed outcomes disagree on
  coordinates, share a payload value, or repeat an outcome name. The unit
  tests validate its output against the real `payloads/schema.json`, so
  the tool can never emit a block the loader would reject.
- `render_profile_snippet(name, block)` — the paste-ready JSON.

---

## Safety model

This tool sends **unverified candidate commands** to a real camera —
a meaningfully higher risk than `send_record_command.py` replaying a
known-good command. Mitigations, in order:

1. `--model-key`/`--firmware` are **required, no defaults** — no accidental
   sweep against the default G2.
2. Phase B prints every candidate with its exact TX bytes and requires a
   typed `yes` before the first write (`tools/control/`'s other tools have
   no confirmation prompt; this one is the deliberate exception — see
   `docs/active_camera_control.md`).
3. One candidate at a time, each followed by the operator-confirm menu
   (`[1] start [2] stop [n] nothing [r] repeat [q] quit`) — the sweep never
   runs ahead of the operator's understanding of camera state.
4. After a state-changing confirmation the camera is restored to idle:
   automatically when `--restore-value` is given (skipped when the
   confirmed value already *is* the restore value), otherwise via an
   explicit "restore then press Enter" prompt.
5. The full capture session — every candidate window and restore window —
   is saved to `tools/captures/` as evidence, whether or not anything was
   confirmed.
6. The tool **never edits the profile JSON**: the operator pastes the
   emitted block and `pytest tests/unit` validates it, keeping a human and
   the schema between discovery and production use.

The **operator confirmation, not the echo, is ground truth**. On an
unfamiliar camera the echo may not decode at all (`decode_packet` rejects
unknown operation/data-type bytes; the capture still records the raw hex
with a `decode_error`). A confirmed outcome without a decodable echo simply
emits a block without `echo_operation` — the schema permits that.

---

## Worked example: recording on POCKET_6K_PRO v8.6

The reason this tool exists. Step by step on the real camera:

```
# 1. Passive capture: toggle record on the camera body during the two windows
python tools/sniffers/sniffer_recording.py --model-key POCKET_6K_PRO --firmware v8.6

# 2. Guided sweep, seeded from that capture.
#    values 2,0: the G2 (and SDI spec transport-mode) suggest 2=record, 0=preview
#    reserved 1,0: the G2's real command needed reserved=1 — try that first
#    restore-value 0: auto-send the suspected stop value after each hit
python tools/control/discover_command.py \
    --model-key POCKET_6K_PRO --firmware v8.6 \
    --label recording \
    --from-capture tools/captures/POCKET_6K_PRO_v8.6/<capture>.json \
    --values 2,0 --reserved 1,0 \
    --outcomes start,stop \
    --restore-value 0

# 3. Paste the emitted "recording" block into
#    payloads/models/POCKET_6K_PRO_v8.6.json's "commands" map
python -m pytest tests/unit    # schema-validates the pasted block

# 4. Confirm end-to-end through the verified session path
python tools/control/send_record_command.py --model-key POCKET_6K_PRO --firmware v8.6
python examples/record_start_stop.py   # adjust model/firmware constants
```

The G2-derived seeds (`--values 2,0`, `--reserved 1,0`) are *hypotheses to
test on the PRO*, not values to copy — that is exactly what the sweep is
for (CLAUDE.md design principle 6). If the PRO deviates, the sweep finds
its real values; if it matches, the capture is the per-model proof the
principle requires.

Note the emitted block's provenance is `status: "VERIFIED"` with
`method: "guided-discovery"` — the operator physically watched the camera
act. Repeat cycles through `examples/record_start_stop.py` (step 4) remain
the bar for calling the *session path* verified, mirroring how the G2 was
done (3/3 cycles).

---

## Testing

`tests/unit/tools/common/test_discovery.py` covers: candidate sweep order
and operator-ordering preservation; `CandidateCommand.encode` parity with
`encode_assign` (including a byte-for-byte match with the known G2 start
packet); capture seeding with and without ambient filtering, single-window
behaviour, and skipping of non-INCOMING_CONTROL/undecoded notifications;
echo extraction; block building — including validation of emitted blocks
against the real `payloads/schema.json` — and every rejection path; snippet
JSON round-trip. `tests/unit/protocol/test_codec.py` covers `encode_assign`
directly.
