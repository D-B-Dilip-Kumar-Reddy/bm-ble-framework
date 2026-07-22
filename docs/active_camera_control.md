# Active Camera Control

**Status:** implemented — `write_outgoing_control` + `run_send_and_capture` + `tools/control/` are live.

## Overview

Unlike `tools/query/*.py` (read-only inspection) and `tools/sniffers/*.py`
(passive, listen-only capture), tools under `tools/control/` **actively send
commands to a real camera** — they will actually change its state (e.g.
start or stop recording). This doc covers the two pieces that make that
possible: `BMDCameraController.write_outgoing_control` (the raw BLE write)
and `run_send_and_capture` (the capture-engine mode that sends a command and
then listens for a fixed duration).

Use these deliberately. There is no confirmation prompt before the write —
running a `tools/control/*.py` script against a real camera performs the
action. Two deliberate exceptions gate their writes behind a typed `yes`,
because what they send is not a VERIFIED profile command:
`tools/control/discover_command.py` (sends *unverified candidate* commands,
plus per-candidate operator confirmation — see `docs/command_discovery.md`)
and `tools/control/send_settings_command.py` (sends CANDIDATE-provenance
settings commands — see below and `docs/settings.md`).

---

## `BMDCameraController.write_outgoing_control` (`src/bmd_ble/camera_controller.py`)

```python
async def write_outgoing_control(self, data: bytes) -> None
```

Pure BLE transport — no BMD protocol knowledge, matching this file's existing
scope (connect/disconnect, raw read/write, subscribe). Logs TX bytes at
`DEBUG` as uppercase hex pairs (CLAUDE.md's BLE byte logging convention)
before writing to `CHARACTERISTIC_OUTGOING`. Raises `RuntimeError` if not
connected; re-raises `BleakError`/`OSError` from the underlying write.

**This method does not verify the write took effect.** It only confirms the
BLE write itself succeeded at the transport level — confirming the command
actually changed camera state (via an echo or `CAMERA_STATUS`) is the
caller's responsibility, per CLAUDE.md's verification-first design principle.
No such verification is wired up yet; today's `tools/control/*.py` scripts
just capture and report whatever arrives, they don't assert success.

---

## `run_send_and_capture` (`tools/common/capture.py`)

```python
async def run_send_and_capture(
    cam: BMDCameraController,
    actions: list[tuple[str, bytes]],
    *,
    listen_seconds: float = 3.0,
) -> CaptureSession
```

For each `(label, command_bytes)` pair: subscribes the shared capture
callback (same `INCOMING_CONTROL`/`CAMERA_STATUS`-only subscription
`run_capture_windows` uses — see `docs/sniffer_capture_engine.md`), marks the
window "hot", calls `cam.write_outgoing_control(command_bytes)`, sleeps
`listen_seconds` to let any response accumulate, then closes the window and
prints its summary. Reuses every existing decode/dedupe/report/save
primitive unchanged — the only difference from `run_capture_windows` is what
starts and ends a window (a command + fixed sleep, not two `input()` prompts).

If nothing arrives within `listen_seconds`, the summary explicitly shows
"(none observed)" / "0 notifications" rather than silently reporting success
— absence of a response is itself useful information (e.g. it might mean the
category isn't echoed at all, or the timing needs adjusting).

---

## `tools/control/send_record_command.py`

The first consumer. Builds the record start/stop command bytes from the
profile's `commands.recording` block (never hardcoded — see
`payloads/models/POCKET_6K_G2_v7.9.json` and `docs/recording.md`), connects,
sends `record_start`, waits `--hold-seconds` (so something is actually
recorded), sends `record_stop`, and saves the combined capture.

This is the tool that closes `docs/recording.md`'s previously-deferred
"deterministic send-then-observe round trip" item — two prior passive
captures already found the same echo signature (category `0x0A`/parameter
`0x01`, payload leading byte 2/0) by coincidental timing; this tool produces
that same evidence on demand, on command, rather than waiting for a
manually-triggered action to land inside a passive capture window.

Requires the profile's `commands.recording` block to be fully populated
(`profile.require_command("recording", ("start", "stop"))`) — raises a clear
`ValueError` naming the missing block or value names otherwise, rather than
sending a malformed command.

---

## `tools/control/send_settings_command.py`

Sends one of the three CANDIDATE settings families (`codec_quality`,
`video_format`, `recording_format` — byte layouts and value tables in
`docs/settings.md`) built entirely from the profile's command blocks plus
its `codecs`/`resolutions`/`fps_modes` lookup tables, then captures the
response via `run_send_and_capture`.

Unlike `send_record_command.py` — and like `discover_command.py` — it
**gates the write behind a typed `yes`** after printing the exact TX bytes:
these families are CANDIDATE (transcribed from an external
reverse-engineering document, never confirmed by this repo's tooling on any
camera), so sending one carries discovery-grade risk, not replay-grade
risk. It also requires explicit `--model-key`/`--firmware` with no
defaults, for the same reason. The operator watching the camera body is
ground truth for what changed; the saved capture is the evidence that
promotes (or falsifies) the profile block's provenance — see
`docs/settings.md`'s verification runbook, including the two-run experiment
that tests the "codec_quality doesn't switch codec families, video_format
does" claim.

`--dimension-enum 0x..` puts the video_format packet into **probe mode**:
it sends a raw candidate enum instead of looking one up from the profile.
This exists because the 2026-07-20 passive capture proved dimension enums
never appear in notifications — the camera reports settings state on
`0x01/0x09` and `0x0A/0x00`, never `0x01/0x00` — so a missing
(resolution, codec) enum (e.g. 4K DCI ProRes) can only be mapped by
actively sending candidates one at a time, with the operator noting what
the camera switches to and the captured `0x01/0x09` report supplying the
resulting width/height (`docs/settings.md` §4.1, §5).

**`--connect-settle-seconds` (default `6.0`s).** Added 2026-07-20 after this
tool's first three real-hardware runs each captured the camera's
post-connect initial-payload burst instead of a response to the write — the
same race `CameraSession.__aenter__`'s `connect_settle_s` wait exists to
prevent (`docs/session_and_verification.md`), which this standalone tool
had never applied since it doesn't go through `CameraSession`. `run()` now
waits this long after `cam.connect()`, before `run_send_and_capture` opens
its window, so the burst fully drains first. Full writeup of the three
confounded captures and what they still established (via operator
confirmation, independent of the bad capture) is in `docs/settings.md` §6.
`tools/control/discover_command.py` has the same latent risk on its first
candidate only — see `docs/command_discovery.md`'s safety model.

**`--repeat N` (default `1`) — the redundant-write echo probe.** Added
2026-07-21. Real-hardware evidence proved `codec_quality`'s report only
fires on an *applied* change: requesting the (codec, variant) the camera
is already at produces no echo at all (`docs/settings.md` §11 —
`CameraSession.set_codec_quality` now guards against this via
`last_known_codec_variant`). Whether `video_format` and `recording_format`
share that same silent-no-op behavior is an open question (`docs/settings.md`
§13) — this flag exists to answer it with a real capture rather than a
guess. With `--repeat 2`, the tool sends the exact same command bytes
twice in one connected session — via `build_repeated_actions`, which just
duplicates the `(label, command)` pair `run_send_and_capture` already
takes a list of, suffixing each with `(send i/N)` — so each send gets its
own labeled capture window and `print_window_summary`:

```
python tools/control/send_settings_command.py \
    --model-key POCKET_6K_G2 --firmware v7.9 \
    --packet recording_format --resolution "4K DCI" --fps 25 --repeat 2

python tools/control/send_settings_command.py \
    --model-key POCKET_6K_G2 --firmware v7.9 \
    --packet video_format --resolution UHD --codec ProRes --fps 25 --repeat 2
```

Send 1 lands the camera in the target state and should echo normally, same
as any other run of this tool. Send 2, requesting a state the camera is
already in, is the deliberate probe: `(none observed)` on that window's
summary reproduces the `codec_quality` finding for this family too (a
`last_known_*` no-op guard belongs in the matching `CameraSession` method,
mirroring `last_known_codec_variant`); a normal echo on both windows means
that family reports unconditionally and needs no such guard.

---

## `tools/control/sweep_dimension_enum.py`

Added 2026-07-22 to make `video_format`'s `--dimension-enum` probe mode
(above) practical to run exhaustively. Sending one candidate at a time via
`send_settings_command.py` means re-scanning and reconnecting for every
value (~15-20s of overhead each) and leaves match detection to the
operator watching the camera body — unreliable on at least one camera:
`POCKET_6K_PRO v8.6`'s on-screen display does not live-update after a
`video_format` write until power-cycled, even though the write
demonstrably takes effect (`docs/settings.md` §15), so an operator
watching the screen during a sweep sees nothing change on every single
candidate, match or not.

This tool connects **once** and sends every candidate `dimension_enum` in
the sweep into its own labeled capture window (reusing
`run_send_and_capture` exactly like `send_settings_command.py` and
`discover_command.py` do), then decodes each window's `recording_format`
(`0x01/0x09`) and `codec_quality` (`0x0A/0x00`) reports straight from the
wire via `protocol/categories/settings.py`'s existing `decode_recording_format`
/`decode_codec_quality` — the same evidence this repo already treats as
ground truth elsewhere, not the on-screen display. Given
`--target-resolution` (and optionally `--target-codec`), it flags a match
automatically:

```
python tools/control/sweep_dimension_enum.py \
    --model-key POCKET_6K_PRO --firmware v8.6 \
    --fps 25 --target-resolution "4K DCI" --target-codec ProRes
```

With no `--enums`/`--range`, the default sweep range is `0x00`-`0x16`
(matching the range the G2's own exhaustive 4K DCI/ProRes search covered,
`docs/settings.md` §7-§8), minus every `dimension_enum` value already
present in the profile's `resolutions` table — no point resending a value
whose target is already known; `--include-known` overrides this. Like
`discover_command.py`'s sweep, this is typed-yes gated **once** for the
whole plan (not per candidate) — reviewing the printed candidate list
before confirming is the operator's chance to trim it with
`--enums`/`--range`/`--include-known` first, since every candidate is a
real, unverified write. `--stop-on-match` (default on) asks whether to
stop as soon as a match is found rather than continuing to burn through
the rest of the range; `--restore-enum` optionally sends one more write
at the end to leave the camera in a known state. If the profile hasn't
reverse-engineered `recording_format`/`codec_quality` yet (early Phase 3),
automated match detection is disabled and the tool falls back to raw-hex
capture evidence only, same as this file's other tools in that situation.

Motivating case: `docs/settings.md` §16 — `POCKET_6K_PRO v8.6` has no known
`dimension_enum` for ProRes/4K DCI, and a passive capture confirmed the
camera genuinely holds and reports that state when reached by hand through
the body menu, so the state is real and an exhaustive sweep is the most
promising way to find whatever `dimension_enum` (if any) reaches it
directly through this codebase's own writes.

---

## `tools/control/discover_command.py`

The second consumer of `run_send_and_capture`, for the opposite situation:
the profile block does **not** exist yet and the command must be
reverse-engineered. It sweeps operator-supplied candidate values/reserved
bytes over a seeded (category, parameter, data_type), asks the operator to
confirm what the camera physically did after each send, and emits a
ready-to-paste `commands` block. Full writeup: `docs/command_discovery.md`.

---

## Why a separate `tools/control/` folder

`tools/` is organized by what a tool *does*, not by feature:

| Folder | What it does | Changes camera state? |
|---|---|---|
| `tools/query/` | Read-only characteristic/metadata inspection | No |
| `tools/sniffers/` | Passive BLE-notification capture (listen-only) | No |
| `tools/control/` | Actively sends commands, captures the response | **Yes** |
| `tools/common/` | Shared library code used by more than one of the above | N/A (not an entrypoint) |

Sending a command is a meaningfully different risk profile than reading or
listening, so it gets its own folder rather than being folded into
`sniffers/` — see CLAUDE.md's "Package Structure" section.
