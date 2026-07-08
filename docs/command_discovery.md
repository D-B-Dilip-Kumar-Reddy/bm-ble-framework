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

The reason this tool exists, walked through step by step on the real camera.

### Step 1 — Get a passive capture first (recommended)

Before sweeping candidates, capture what the camera reports while a human
triggers the action on the camera body. This seeds the sweep with the right
`(category, parameter, data_type)` instead of guessing blind.

```bash
python tools/sniffers/sniffer_recording.py --model-key POCKET_6K_PRO --firmware v8.6
```

This prompts you to press Enter, physically start recording on the camera,
press Enter again, then repeat for stop. It saves a JSON file under
`tools/captures/POCKET_6K_PRO_v8.6/<timestamp>.json` — note that path.

(You can skip this and pass `--category`/`--parameter`/`--data-type`
manually instead if you already know the coordinates from
`docs/protocol.md`'s `[spec]` tables.)

### Step 2 — Run the discovery sweep

```bash
python tools/control/discover_command.py \
    --model-key POCKET_6K_PRO --firmware v8.6 \
    --label recording \
    --from-capture tools/captures/POCKET_6K_PRO_v8.6/<the-file-from-step-1>.json \
    --values 2,0 \
    --reserved 1,0 \
    --outcomes start,stop \
    --restore-value 0
```

Flag meaning:

| Flag | Meaning |
|---|---|
| `--model-key` / `--firmware` | Required, no defaults — forces you to be explicit about which camera. |
| `--label` | Name for the emitted block (`recording`, `photo`, whatever command family you're discovering). |
| `--from-capture` | The JSON from step 1 (or omit and use `--category`/`--parameter`/`--data-type` instead). |
| `--values` | Comma-separated payload values to try, most-likely first. `2,0` mirrors the G2's known start/stop values. |
| `--reserved` | Comma-separated reserved-byte values to try. Default `0`; the G2 needed `1`, so try `1,0` on another Pocket-line camera. |
| `--outcomes` | Names you'll be asked to confirm, matched 1:1 to what you expect the values to do. |
| `--restore-value` | Optional; auto-sends this value after a state-changing hit to put the camera back to idle (e.g. `0` = stop) instead of prompting you to restore it by hand. |

### Step 3 — Confirm the sweep plan

The tool prints every candidate it's about to send (exact TX bytes) and asks:

```
Type 'yes' to proceed:
```

This is your last chance to bail before anything touches the camera — type
`yes` to continue.

### Step 4 — Connect and probe candidates one at a time

It scans for and connects to the camera, then for each candidate:

1. Sends the command.
2. Listens for `--listen-seconds` (default 3s) and shows you the decoded
   response.
3. Asks:
   ```
   What did the camera physically do?
     [1] start  [2] stop  [n] nothing  [r] repeat  [q] quit sweep:
   ```
4. **Look at the physical camera** and answer based on what it actually
   did — not the echo. Type the number matching the outcome, `n` if
   nothing happened, `r` to resend the same candidate, or `q` to stop early.

After a confirmed state-change, the camera is either auto-restored (via
`--restore-value`) or you'll be prompted to restore it manually (e.g. stop
recording on the body) before the next candidate.

### Step 5 — Get the emitted block

Once you've confirmed all outcomes (or quit early), it:

1. Saves the full capture (all candidates + responses) to `tools/captures/`
   as evidence.
2. Prints a ready-to-paste JSON block, e.g.:

```json
{
  "recording": {
    "category": 10,
    "parameter": 1,
    "data_type": "BOOL",
    "reserved": 1,
    "values": { "start": 2, "stop": 0 },
    "echo_operation": 2,
    "provenance": {
      "status": "VERIFIED",
      "method": "guided-discovery (tools/control/discover_command.py)",
      "capture_refs": ["tools/captures/POCKET_6K_PRO_v8.6/..."],
      "verified_on": "2026-07-08",
      "notes": "Operator-confirmed outcomes: start, stop."
    }
  }
}
```

### Step 6 — Paste it into the profile and validate

Copy that block into `payloads/models/POCKET_6K_PRO_v8.6.json`'s
`"commands"` map (replacing the placeholder `_comment`), then run:

```bash
python -m pytest tests/unit
```

This validates the pasted block against `payloads/schema.json` immediately
— if you made a typo or the shape's wrong, it fails here rather than at
camera-connect time.

### Step 7 — Confirm end-to-end through the real session path

```bash
python tools/control/send_record_command.py --model-key POCKET_6K_PRO --firmware v8.6
python examples/record_start_stop.py   # adjust model/firmware constants
```

This replays start/stop using the profile block you just added and
captures the response, closing the loop from "discovered" to "usable by
`CameraSession`."

### Notes

The G2-derived seeds (`--values 2,0`, `--reserved 1,0`) are *hypotheses to
test on the PRO*, not values to copy — that is exactly what the sweep is
for (CLAUDE.md design principle 6). If the PRO deviates, the sweep finds
its real values; if it matches, the capture is the per-model proof the
principle requires.

Note the emitted block's provenance is `status: "VERIFIED"` with
`method: "guided-discovery"` — the operator physically watched the camera
act. Repeat cycles through `examples/record_start_stop.py` (step 7) remain
the bar for calling the *session path* verified, mirroring how the G2 was
done (3/3 cycles).

A caveat worth expecting on an unfamiliar camera: if the PRO's echo uses
operation/data-type bytes this codebase doesn't recognize yet, step 4's
decoded response may show a decode error instead of clean output — that's
fine, your own eyes on the camera are what matter for confirmation, and the
raw hex is still captured as evidence either way (see "Safety model" above).

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
