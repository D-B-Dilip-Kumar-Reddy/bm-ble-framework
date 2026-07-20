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
