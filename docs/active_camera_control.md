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

**`--data-type NAME` — probe the CANDIDATE-vs-spec data-type discrepancy.**
Added 2026-07-23 (`docs/settings.md` §3/§4/§16). The `recording_format`
write byte (`0x82`/`INT16_ARRAY`) has never matched the camera's own
REPORT byte for the same category/parameter (`0x02`/`INT16`) — flagged as
an open hypothesis since §4.2 ("if `0x82` is rejected, try `0x02`") and
given a concrete motivating failure by §16: `POCKET_6K_PRO v8.6` never
confirms a `recording_format` retarget to 4K DCI while ProRes is active,
2/2 real round trips, even though the target state is independently
proven real via passive capture. `--data-type NAME` (any `DataType`
member name, e.g. `INT16`) overrides `build_command()`'s data_type for
whichever `--packet` is selected — generic across all three families, not
just `recording_format`, since the discrepancy this flag probes is a
property of the wire byte itself. Default: unset, uses the profile's own
value unchanged, so every existing invocation stays byte-for-byte
identical to before this flag existed. `INT16` and `INT16_ARRAY` share the
identical struct format/byte width (`protocol/types.py`), so this changes
only packet header byte 6, never the payload encoding or length. The
override is recorded in the saved capture's label
(`data_type=INT16(0x02 override; profile default INT16_ARRAY/0x82)`) so a
later reviewer can tell at a glance which captures used a non-profile
byte:

```
python tools/control/send_settings_command.py \
    --model-key POCKET_6K_PRO --firmware v8.6 \
    --packet recording_format --resolution "4K DCI" --fps 25 \
    --data-type INT16
```

If this is confirmed accepted on real hardware, promoting
`payloads/models/POCKET_6K_PRO_v8.6.json`'s
`commands.recording_format.data_type` to `INT16` is a separate,
evidence-gated follow-up — this flag only makes the experiment possible.

**Update, 2026-07-23/24:** real hardware ruled this hypothesis out for the PRO's
ProRes/4K DCI gap — `--data-type INT16` with `--listen-seconds 8` produced zero
fresh confirming reports, the same signature already established for `0x82`
(`docs/settings.md` §16). The write-byte axis is exhausted.

**`--video-format-extra E1 E2` — probe `video_format`'s unexplained trailing
elements.** Added 2026-07-24 (`docs/settings.md` §16). `video_format`'s payload is
`[fps_int, m_rate, dimension_enum, extra1, extra2]` — every capture on either camera
shows `extra1`/`extra2` as `0, 0` (hypothesis: the official spec's
`interlaced`/`colorspace` video-mode elements), and `encode_video_format` always
hardcoded them until now. With the `dimension_enum` sweep exhausted (`0x00`-`0x1F`)
and the `recording_format` data-type hypothesis ruled out, these two bytes are the
last untried lead from the original candidate list. `--video-format-extra E1 E2`
(accepts `0x..` hex or decimal) overrides `build_command()`'s `extra1`/`extra2` for a
`video_format` send — mirroring `--dimension-enum`/`--data-type`'s pattern exactly:
default unset (every existing invocation stays byte-for-byte unchanged), and the
override is recorded in the send's label
(`extra=(1,0) override; profile default (0,0)`) for the same evidentiary
traceability:

```
python tools/control/send_settings_command.py \
    --model-key POCKET_6K_PRO --firmware v8.6 \
    --packet video_format --resolution UHD --codec ProRes --fps 25 \
    --video-format-extra 1 0
```

Not yet tried on real hardware — this makes candidate 3 testable, it isn't a result.
As with the other probe flags, watch for a matching `recording_format`/
`codec_quality` report rather than the on-screen display, and use a generous
`--listen-seconds` given this camera's documented lens-burst timing confound.

**Update, 2026-07-24:** tried against `(UHD, ProRes, 25fps)` with four `(extra1,
extra2)` pairs. `(1, 0)` confirmed 2/2 — safe, but still landed UHD, not 4K DCI.
`(2, 0)`, `(0, 1)`, and `(1, 1)` were each silently rejected over a full 10s window
— the same signature the `dimension_enum` sweep's invalid candidates showed, not
`recording_format`'s "accepted but unconfirmed" one. No support for this hypothesis
either; see `docs/settings.md` §16 for the full write-up and what's left to try.

All three original candidate hypotheses for the PRO's ProRes/4K DCI gap are now
exhausted. A full-channel decode of the passive-capture evidence (`docs/settings.md`
§16) — every notification in the body-menu-driven transition windows, not just
`recording_format`/`codec_quality` — found nothing new either: no channel besides
`recording_format` itself correlates with the transition. That was the strongest
remaining lead, and it's exhausted too.

**`--operation NAME` — probe the one untested wire axis left.** Added 2026-07-24
(`docs/settings.md` §16). Every write attempted across all three exhausted
hypotheses above used `Operation.ASSIGN` (packet header byte 7 = `0x00`,
`protocol/codec.py`). The header format documents a second write-capable operation,
`OFFSET` (`0x01`), never tried for any settings family on either camera — its
semantics for a resolution/format field are unknown, but it varies a genuinely
different axis than value, data-type byte, or trailing elements. `--operation NAME`
(any `Operation` member name, e.g. `OFFSET`) overrides `build_command()`'s operation
byte for whichever `--packet` is selected — generic across all three families, like
the other override flags. Default: unset, uses `Operation.ASSIGN` unchanged, so
every existing invocation stays byte-for-byte identical to before this flag existed.
The override is recorded in the send's label
(`operation=OFFSET(0x01 override; profile default ASSIGN/0x00)`):

```
python tools/control/send_settings_command.py \
    --model-key POCKET_6K_PRO --firmware v8.6 \
    --packet recording_format --resolution "4K DCI" --fps 25 \
    --operation OFFSET
```

Not yet tried on real hardware. As with the other probe flags, use a generous
`--listen-seconds` given this camera's documented lens-burst timing confound.

**Update (2026-07-24):** tried on real hardware — an absolute-target `OFFSET` write
(`--operation OFFSET` with the same values `--resolution "4K DCI" --fps 25` produces
under `ASSIGN`) got zero response over a 10s listen window: no `0x01/0x09` report, no
report on any channel besides the ambient `0x09/0x00` storage telemetry (`docs/
settings.md` §16). That doesn't itself rule `OFFSET` out — `docs/protocol.md` §4
documents its spec meaning as "add the payload to the current value," so sending an
*absolute* target as an `OFFSET` isn't a faithful test of that semantics. See
`--raw-payload` below for the delta-payload test this motivates.

**`--reserved BYTE` — vary the least-evidenced field in a CANDIDATE block.**
Added 2026-07-30 (`docs/settings.md` §18.6). Packet header byte 3 was the one axis
this tool could not change without editing a profile. It deserves a flag because it
is the *least*-evidenced field in any block seeded from a passive capture: a camera's
own REPORT packets need not carry the value a **write** requires — the same trap
`recording_format`'s `0x02`-vs-`0x82` discrepancy already represents on the data-type
axis (`docs/settings.md` §3).

That is not hypothetical. On `POCKET_6K_G2 v8.6` the recording family accepts **both**
`0x00` and `0x01` (`docs/recording.md`), every report on that firmware carries `0x00`,
and v7.9's working writes used `0x01` — three different answers for one byte. So when
a CANDIDATE block's write draws no response, this is the first thing to vary, before
doubting the payload values:

```
python tools/control/send_settings_command.py \
    --model-key POCKET_6K_G2 --firmware v8.6 \
    --packet video_format --fps 24 --dimension-enum 0x08 --reserved 0x00
```

Accepts `0x..` hex or decimal, applies to all three packet families, composes with
every other override, and records itself in the send's label
(`reserved=0x00(override; profile default 0x01)`) so a later reviewer can tell which
captures used a non-profile byte. Default: unset, uses the profile block's own value,
so every invocation predating this flag is byte-for-byte unchanged.

**`--raw-payload VALUE [VALUE ...]` — send a literal payload, bypassing every
lookup table.** Added 2026-07-24 (`docs/settings.md` §16, `docs/protocol.md` §4).
Every override above still builds most of the payload from `--resolution`/`--codec`/
`--fps` via the profile's lookup tables, changing only one field (data-type byte,
trailing elements, operation byte). Testing `OFFSET`'s documented "add to current
value" semantics needs something none of those can do: a *delta* payload, not an
absolute target — retargeting `recording_format` from UHD (3840×2160) to 4K DCI
(4096×2160) via `OFFSET` means sending a width delta of `4096-3840=256`, not the
absolute width `4096`. `--raw-payload` (accepts `0x..` hex or decimal per element)
bypasses `--resolution`/`--codec`/`--fps`/`--sensor-fps` and the lookup tables
entirely, encoding the literal element sequence as the payload — still reading
category/parameter/reserved from the profile's command block for `--packet`, and
still composing with `--data-type`/`--operation`. It calls `encode_assign_elements`
(`protocol/codec.py`) directly, the same generic encoder every `encode_*` wrapper in
`protocol/categories/settings.py` already delegates to, so no protocol-layer changes
were needed. Default: unset, every existing invocation unaffected:

```
python tools/control/send_settings_command.py \
    --model-key POCKET_6K_PRO --firmware v8.6 \
    --packet recording_format --raw-payload 0 0 256 0 0 \
    --operation OFFSET --listen-seconds 10
```

The five elements match `recording_format`'s `[fps_int, sensor_fps_int, width,
height, frame_flags]` shape — `0` for the fields with no requested change, `256` for
the width delta. `--raw-payload` does no per-family validation of element count or
meaning — that's on the caller, matching `--dimension-enum`'s and
`--video-format-extra`'s existing stance.

**Update (2026-07-24):** tried on real hardware — same zero-response signature as
the absolute-payload `OFFSET` test: no `0x01/0x09` report in a full 10s window, TX
independently confirmed correct (`operation=0x01`, payload decodes to
`(0, 0, 256, 0, 0)`). This result is stronger evidence than the absolute-payload
test, since a `+256` width delta from UHD (3840) lands exactly in-range at 4096 —
the "out-of-range absolute value" explanation for the earlier silence doesn't apply
here. Every hypothesis raised for the PRO's ProRes/4K DCI gap (`dimension_enum`
sweep, `data_type` byte, `video_format` trailing elements, full-channel passive
decode, `OFFSET` absolute payload, `OFFSET` delta payload) is now exhausted with no
confirming echo. See `docs/settings.md` §16 for the full write-up and the next
diagnostic step under consideration.

**Update (2026-07-24):** the follow-up isolating test — an `OFFSET` delta against
`codec_quality` instead of `recording_format`
(`--packet codec_quality --raw-payload 0 1 --operation OFFSET`), a family whose
`ASSIGN` echo behavior is already well-characterized (fires on a real change,
silent only on an exact no-op) — also got zero response over a full 10s window, TX
independently confirmed correct. Since a `+1` variant delta is not a no-op, this
rules out "`OFFSET` is refused specifically for `recording_format`" and points at
`Operation.OFFSET` not being acted on by this camera's firmware at all, for either
category/parameter tried so far. See `docs/settings.md` §16 for the full write-up.

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

A second, structurally different use case (`docs/photo_capture.md` §10.1's
"Next steps"): hunting for a *second* `dimension_enum` that aliases to a
resolution already known, rather than one reaching an unknown resolution.
Both profiles record exactly one ProRes/HD enum (`3`); the Sensor Area
investigation (§10.1/§10.3) found that a still-photo sensor readout choice
sits underneath the displayed video resolution without changing
`recording_format`'s width/height at all, only its flags nibble — so if
Sensor Area is written via `video_format` the way ordinary resolution
changes are, its enum bytes would all decode to the *same* `HD`
width/height as the one already known, distinguishable only by that flags
bit. This needs `--no-stop-on-match` (the tool's default stops at the
first match, which would hide a second/third one) and `--include-known`
(to get a fresh baseline flags reading from the already-known enum in the
same session) — the opposite defaults from the motivating case above,
since here every match matters, not just the first one. **Run on
`POCKET_6K_PRO v8.6`, 2026-07-27 (`docs/photo_capture.md` §10.5): the
result was a genuine methodology lesson, not a confirmed second enum** —
see the stale-match guard below.

**Stale-match guard (added 2026-07-27, from that same run).** `is_match`
only ever checked a candidate's decoded state against the caller's
*target* — nothing checked whether that state was actually *caused* by
the candidate, versus already being true before the write (an invalid or
unassigned enum still gets a report; the camera just reflects whatever it
already held, the same "report isn't an ack, it's a state reflection"
mechanism `docs/settings.md` §7 already established for `video_format`
writes generally). Candidate `0x00` demonstrated this directly: it
"matched" `HD` in one sweep and a completely different resolution in an
identical immediate rerun, both times exactly reproducing whatever the
camera held right before `0x00` was sent — not a result `0x00` itself
produced. The tool now tracks the last confirmed `(width, height, flags)`
state across candidates (carried forward through silent ones) and flags
any `MATCH` identical to it as a **possible stale match**, both inline
during the sweep and in the closing summary, rather than reporting it
as an unqualified hit.

---

## `tools/control/sweep_camera_format.py`

Added 2026-07-24, directly motivated by how the ProRes/4K DCI gap above was
actually found: by accident, mid-investigation of something else, on a
combination nobody had set out to test. Closing it took a full day of
targeted hypothesis testing before being accepted as a genuine software
capability gap (`resolutions.<name>.known_unreachable`, `docs/payload_profiles.md`)
— and nothing in this codebase's tooling checked whether a similar gap
exists on any of the profile's *other* combinations. A `POCKET_6K_PRO
v8.6`-sized profile claims support for around 480 `(codec, variant,
resolution, fps)` combinations; this tool is what makes checking all of
them (or a chosen slice) practical instead of hoping the next gap surfaces
by accident too.

Unlike every other tool in this file, it runs `CameraSession.set_camera_format()`
— the real production API — rather than building raw protocol packets
directly. That is a deliberate precedent break (see the tool's own module
docstring for the full rationale): its entire purpose is verifying the
production code path end to end, including `set_camera_format`'s no-op
guards, its `known_unreachable` precondition check, and its proxy-resolution
logic — testing any of that at the raw protocol level would exercise a
parallel implementation, not the one this tool exists to check.

```
# Always start here — preview the plan, no connection made:
python tools/control/sweep_camera_format.py \
    --model-key POCKET_6K_PRO --firmware v8.6 --dry-run

# A practical, narrowed sweep (the full ~480-combination sweep is real,
# but --resolutions/--codecs/--variants/--fps make it tractable to run in
# focused slices):
python tools/control/sweep_camera_format.py \
    --model-key POCKET_6K_PRO --firmware v8.6 \
    --resolutions "4K DCI,UHD" --codecs ProRes --fps 25,24
```

`enumerate_combinations` builds the combination list straight from the
profile's `resolutions`/`codecs`/`fps_modes` tables — the same tables
`set_camera_format` itself reads — in profile-declaration order (dict
iteration order, matching JSON key order, so repeated runs produce the same
plan). A `(codec, resolution)` pair already listed in that resolution's
`known_unreachable` map is skipped by default, since its outcome is already
known and would just raise `BMDUnsupportedError` immediately; `--include-known-unreachable`
overrides this, e.g. to re-verify one after a suspected fix. Filters are
validated against the profile up front (the same `require_resolution`/
`require_codec`/`require_fps_mode` fail-loud contract as everywhere else in
this codebase) — an unknown name in `--resolutions`/`--codecs`/`--fps`
raises immediately rather than silently sweeping zero combinations.

Each combination's `set_camera_format()` call is classified into one of four
outcomes:

| Outcome | Meaning |
|---|---|
| `confirmed` | Every step echo-verified (or correctly recognized as an already-satisfied no-op) — the combination genuinely works. |
| `unsupported` | `BMDUnsupportedError` — the camera doesn't offer this codec at this resolution, this fps exceeds a resolution's `max_fps_int` hardware ceiling, or it's already a known software gap. |
| `missing_data` | `ValueError` — profile data needed to attempt the write (usually a `dimension_enum`) hasn't been captured yet; not a confirmed failure, just an incomplete profile. |
| `unconfirmed` | `BMDVerificationError` — the write was attempted but never confirmed. **This is the outcome that matters**: a genuine candidate for a new `known_unreachable` entry, the same shape of finding the ProRes/4K DCI investigation eventually confirmed by hand. |

Like `sweep_dimension_enum.py` and `discover_command.py`, this is typed-yes
gated **once** for the whole sweep — the confirmation prompt prints the full
plan and a worst-case time estimate (`3 × echo_timeout_s + pause` per
combination), since a full sweep is genuinely hundreds of real writes.
`--dry-run` is the way to review a plan without that commitment at all. One
failing combination never aborts the sweep — every combination is
independent, and the whole point is a complete picture, not stopping at the
first gap. A structured JSON report (reusing `tools/captures/`'s existing
per-model directory convention, alongside — not through — `tools/common/capture.py`'s
`save_capture`, since this tool's results aren't raw BLE notification
captures) is saved at the end, and every `unconfirmed` result is called out
in the console summary as a `known_unreachable` candidate.

**This tool surfaces candidates — it does not write `known_unreachable`
itself.** An `unconfirmed` result from one sweep run is exactly as strong as
the very first `send_settings_command.py --packet recording_format` run
that originally surfaced the ProRes/4K DCI gap — a lead, not a conclusion.
Promoting one to the profile needs the same real-hardware follow-up that
investigation eventually required before being accepted (`docs/settings.md`
§16): repeat runs from a genuinely different starting state (ruling out a
redundant no-op), the operator confirming on-screen state directly (ruling
out an echo-only confirmation problem), and ideally testing whether the
exact camera-reported value — not just *a* value — was tried, all before
anyone edits the profile. Per CLAUDE.md design principle 6 (sniffer-first),
that review is a human step this tool deliberately does not automate.

**Real-hardware results, first production run (2026-07-24,
`POCKET_6K_PRO v8.6`, `docs/settings.md` §17):** 448 combinations, one
session — 431 confirmed, 17 unconfirmed. A follow-up `--include-known-unreachable`
run (480 combinations) reproduced the identical 17 and correctly classified
all 32 ProRes/4K DCI combinations as `unsupported` — the `known_unreachable`
guard worked exactly as designed. The 17 unconfirmed split into two
findings once checked against the real camera: 16 (`BRAW <every variant>
6K @ 59.94`/`@ 60`) turned out to be a genuine hardware fps ceiling —
confirmed absent from the camera's own UI at 6K, not a write bug — now
modeled as `resolutions.6K.max_fps_int: 50` and excluded from this tool's
default sweep (`--include-unsupported-fps` overrides this, mirroring
`--include-known-unreachable`). The 17th (`ProRes HQ HD @ 23.98`) turned out
to be a false negative in this tool's own default echo timeout (confirmed
successful on-screen despite reporting `unconfirmed`) — the same
lens-metadata-burst confound documented in `docs/session_and_verification.md`,
which is why `--echo-timeout-seconds`'s default was raised from 3.0 to 6.0.
Full write-up: `docs/settings.md` §17.

---

## `tools/control/discover_command.py`

The second consumer of `run_send_and_capture`, for the opposite situation:
the profile block does **not** exist yet and the command must be
reverse-engineered. It sweeps operator-supplied candidate values/reserved
bytes over a seeded (category, parameter, data_type), asks the operator to
confirm what the camera physically did after each send, and emits a
ready-to-paste `commands` block. A VOID seed sweeps payloadless trigger
packets — reserved bytes only, no `--values` — added 2026-07-27 for the
10.3 still-capture probe after the passive photo captures showed there is
no report to seed from (`docs/photo_capture.md`). Full writeup:
`docs/command_discovery.md`.

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
