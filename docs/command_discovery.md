# Guided Command Discovery

**Status:** implemented — used to populate `POCKET_6K_PRO v8.6`'s recording block on real hardware.

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

**Known limitation — scalar payloads only.** The sweep generates
single-value ASSIGN payloads (`encode_assign`), so it cannot probe the
multi-element settings families (codec_quality's id pair, video_format's
five int8 elements, recording_format's five int16s — `docs/settings.md`).
For those, seed the coordinates and value tables from a
`tools/sniffers/sniffer_settings.py` capture, transcribe them into the
profile as CANDIDATE, and confirm by sending the fully-formed packet with
`tools/control/send_settings_command.py` instead.

The same `encode_assign` dependency also means the sweep cannot send a
**void (payloadless)** packet — `encode_assign` deliberately rejects
`DataType.VOID` (`protocol/types.py` omits code 0 from
`DATA_TYPE_STRUCT_FORMATS`). This matters for the photo-capture target: the
[spec] types 10.3 "Still Capture" as void, so if a real capture shows the
trigger genuinely carries no payload, a void-capable encode path must be
built before this tool can sweep it — see `docs/photo_capture.md` §3 for
why that isn't built speculatively.

For the specific case of sweeping many `video_format` `dimension_enum`
candidates against one already-known (category, parameter, data_type) —
this tool's per-candidate re-scan/re-connect and operator-eyeball match
detection don't scale to an exhaustive search — `tools/control/
sweep_dimension_enum.py` is the sibling tool: one connected session, every
candidate decoded straight off the wire (`recording_format`/`codec_quality`
reports), with automated match detection against a target resolution/codec.
See `docs/active_camera_control.md`'s section on it and `docs/settings.md`
§16 for the case that motivated it.

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

  **Known limitation:** this heuristic would also filter out a signal like
  `protocol/categories/storage.py`'s CANDIDATE write-margin warning
  (category `0x09`, parameter `0x01`) — it ticks frequently enough, and its
  category/parameter pair is present across essentially every window, to
  look like ordinary ambient telemetry even though its *value* carries real
  meaning. That signal was found by comparing raw log bytes directly
  (`docs/recording.md`'s "Camera-initiated stop detection"), not through
  this tool. `exclude_ambient` filters on `(category, parameter)` presence,
  not on whether the *values* within a triple vary meaningfully — a future
  improvement here could look for exactly this pattern (a triple present
  everywhere, but whose payload takes on a rare/different value in one
  specific window).
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

**Known latent risk — connect-settle race.** This tool writes the first
candidate immediately after connecting, with no wait for the camera's
post-connect initial-payload burst to drain (the same hazard
`CameraSession.connect_settle_s` exists for — see
`docs/session_and_verification.md`). `tools/control/send_settings_command.py`
hit exactly this on real hardware 2026-07-20 (`docs/settings.md` §6): its
first three captures showed the initial burst instead of a response to the
write, and it was fixed there with a `--connect-settle-seconds` wait. This
tool has not needed the same fix yet — every candidate after the first
naturally waits out `--listen-seconds` plus operator think-time before the
next write, so only the very first candidate is at risk, and the operator's
own eyes (not the echo) are ground truth anyway. Worth the same fix if a
future sweep's first-candidate echo looks suspiciously like unrelated
camera state.

**Sibling tool note.** `send_settings_command.py` gained a `--repeat N`
flag (2026-07-21) for a different discovery question than this tool
answers: not "what command produces this effect" but "does this
already-verified command echo when it's a redundant no-op" (see
`docs/settings.md` §13 and `docs/active_camera_control.md`). This tool has
no equivalent flag — a discovery sweep's candidates are, by construction,
rarely repeats of the same value, so the redundant-write question doesn't
arise here the way it does when replaying one known command twice.
`send_settings_command.py` also gained a `--data-type NAME` override
(2026-07-23, `docs/settings.md` §3/§16, later ruled out on real hardware),
a `--video-format-extra E1 E2` override (2026-07-24, `docs/settings.md`
§16, also ruled out) for probing `video_format`'s unexplained trailing
payload elements, an `--operation NAME` override (2026-07-24,
`docs/settings.md` §16, tried on real hardware for the absolute-payload
case and ruled out — see the next entry for why that's not the whole
story) for probing the `Operation.OFFSET` write, and a `--raw-payload
VALUE [VALUE ...]` override (2026-07-24, `docs/settings.md` §16, tried on
real hardware and also ruled out) that bypasses the profile's lookup
tables entirely to send a literal element sequence — built specifically
to test `OFFSET`'s documented delta semantics (`docs/protocol.md` §4),
which an absolute-target payload can't do, and the delta test came back
with the identical zero-response signature — four unrelated discovery
axes (wire byte, extra payload elements, operation byte, raw payload —
not command bytes), noted here only because the hook that requires
touching this file on any `tools/control/*` change applies to them too.
Every hypothesis this investigation raised for the PRO's ProRes/4K DCI
gap is now exhausted (`docs/settings.md` §16) and accepted as a guarded
software capability gap (`resolutions."4K DCI".known_unreachable.ProRes`,
`docs/payload_profiles.md`). `discover_command.py`
itself still has no equivalent: its `CandidateCommand.encode()` always
uses `Operation.ASSIGN` via `encode_assign`'s (now overridable, but
unused-by-default) `operation` parameter.

A different kind of sibling tool, `tools/control/sweep_camera_format.py`
(2026-07-24, `docs/active_camera_control.md`), exists precisely because that
gap was found by accident rather than by systematic checking: it runs
`CameraSession.set_camera_format()` — the real production API, not a raw
protocol send like every tool above — across every `(codec, variant,
resolution, fps)` combination a profile claims to support, to surface
candidates for future `known_unreachable` entries before a caller hits one
in production. Noted here only because the `tools/control/*` doc-sync hook
applies to it too; it answers a different question than command discovery
("does this already-populated command family actually work for every
claimed combination", not "what command produces this effect" — no
`CandidateCommand` sweep involved at all).

Its first real-hardware run (2026-07-24, `docs/settings.md` §17) found a
genuine camera hardware limit (`POCKET_6K_PRO v8.6`'s `"6K"` resolution
capped at 50fps, now `resolutions.6K.max_fps_int`, excluded from this
tool's default sweep like `known_unreachable`) and a false negative in the
tool's own default echo timeout (fixed by raising `--echo-timeout-seconds`
from 3.0 to 6.0) — same doc-sync-hook note as above, no `CandidateCommand`
involvement either.

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
    "data_type": "INT8",
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
