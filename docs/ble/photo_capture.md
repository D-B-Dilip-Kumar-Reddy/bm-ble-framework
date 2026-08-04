# Photo Capture

**Status:** the trigger command is confirmed on **both cameras**.
`commands.photo` (category `0x0A`, parameter `0x03`, `VOID`) is
`VERIFIED` as of 2026-07-27 in both `payloads/models/POCKET_6K_G2_v7.9.json`
(§7) and `payloads/models/POCKET_6K_PRO_v8.6.json` (§9), each
independently confirmed on its own hardware — a void ASSIGN to that
coordinate reliably fires a real photo capture on both, confirmed by
inspecting the SD card's contents on a PC after each send. **No
BLE-observable signal (echo or otherwise) confirms a photo was taken** on
either camera — every capture window around a confirmed-successful trigger
shows only ambient telemetry, matching the passive finding (§5) that a
body-triggered still produces no report either.

**Phase 6 (2026-08-04) built on top of that instead of waiting for a BLE
signal that structurally doesn't exist**, closing §7.3's open architectural
question — see §11 for the full write-up. `CameraSession.capture_photo()`
(`protocol/categories/media.py`) sends the trigger and, explicitly, nothing
else: it never claims a photo is confirmed, because it cannot. Real
confirmation is `rest/media.py`, the out-of-band REST channel the operator
proposed in §7.3 — it watches for a new still file to appear on the SD
card after the trigger fires. `examples/capture_photo.py` composes both,
holding a BLE session and a REST session open to the same camera at once.
**Implemented and unit-tested; no real-hardware run has been reported
yet** — several of its design choices (§11.3) are still awaiting that
confirmation, most notably the filename-prefix pattern and the concurrent-
BLE-and-REST combination itself.

Path so far: passive sniffing (§5) found no report at all on either
camera; a first active INT8 sweep on the G2 (§6) came back inconclusive
because every candidate was confirmed on operator judgment alone; a VOID
retry on the G2 (§7), this time verified against the SD card's actual
contents rather than a glance, produced the confirmed result; the
identical VOID sweep repeated on the PRO (§9), same SD-card verification
method, reached the identical finding independently. §8 adds
operator-provided (not wire-observed) knowledge of the G2's photo output
dimensions/format: BRAW stills inherit the current recording resolution
(independently cross-confirmed against all six of the profile's BRAW
`resolutions` entries), but ProRes stills use a separate sensor-area
concept (2.8K/5.7K/6K) unrelated to ProRes's own UHD/HD video
resolutions — and every still on the G2 is DNG regardless of codec. §8.4
corrects that last point for the PRO: file format there follows the
active codec, `.braw` for BRAW and DNG for ProRes, not a uniform DNG.
§10's first capture with `tools/sniffers/sniffer_sensor_area.py` found
that changing
Sensor Area does trigger real report activity (`recording_format`,
`codec_quality`, and, on the G2 only, the `0x09/0x02` capacity-shaped
signal) but no directly-encoded sensor-area value on either channel — a
promising but single-sample G2 lead sits in `0x09/0x02`'s monotonic
values (§10.1). §10.3 independently reran the capture on the PRO: same
negative result, plus a genuine cross-model reconfirmation of the
"windowed" flag bit tracking full-sensor-vs-cropped sensor area on both
cameras. §10.4's PRO-only interleaved A-B-A-B repeat then **confirmed the
windowed bit as a clean, reproducible signal** (toggled byte-identically,
twice each way) while putting `0x09/0x02` at a firm 0-for-2 independent
PRO sessions — the G2 side of that question is now permanently untestable
on v7.9, since the operator's G2 has since been upgraded to firmware
v8.6. §10.2 records the operator's cross-model sensor-area option matrix,
including a genuine G2/PRO difference (5.7K vs 5.3K) and both cameras
disabling sensor-area choice entirely at ProRes/4K DCI. §10.5's hunt for
a second `dimension_enum` aliasing to `HD` (the natural next place for
Sensor Area to live) found an apparent match that a same-session repeat
run then **refuted as a stale-state false positive** — a real
methodology lesson, now guarded against in `sweep_dimension_enum.py`
itself. §10.6 then closed the investigation's spec-guided avenue for
good: the operator searched the full official 115-page protocol PDF
directly (every "sensor" occurrence, 26/26) and confirmed no parameter
named or resembling "Sensor Area" exists anywhere in it — the windowed-
mode bit already found (§10.1/§10.3/§10.4) is the *only* officially
documented concept related to sensor readout area in the entire spec,
confirming it as the ceiling of what's discoverable here, not just this
codebase's best guess. §10.7 then closed the one remaining thread: an
isolated write flipping just the windowed bit produced no echo *and* —
confirmed via before/after SD-card photo dimensions, not just wire
silence — no physical effect either. **The Sensor Area investigation is
concluded**: no BLE write path exists for it beyond the read-only
windowed bit, on either camera, by any means tried.

Target camera for first bring-up: `POCKET_6K_G2 v7.9`, per CLAUDE.md's
camera registry ("start all new features with `POCKET_6K_G2 v7.9`") —
note this firmware is no longer available on the operator's own G2 unit
(upgraded to v8.6 as of this session), so further G2 work needs a new
`POCKET_6K_G2_v8.6` profile scaffolded from scratch (CLAUDE.md's
Phase 1-4 workflow) whenever that's picked up.

---

## 1. Spec starting point

From the official SDI category tables (`docs/ble/protocol.md` §5, all **[spec]**
— starting points for a sniffer session, never values to copy into a
profile):

| Coordinate | Name | Type | Meaning |
|---|---|---|---|
| 10.3 | Still Capture | void | capture a photo |

Category 10 (Media) is the same category as this repo's sniffer-verified
recording command (10.1) and codec_quality (10.0), so the *category* byte
`0x0A` appearing on the wire is well precedented on the G2. Parameter `0x03`
has never been observed in any capture so far — neither as a report nor as
ambient telemetry.

Two spec facts to keep in mind while reading a capture:

- **Void trigger.** The spec types 10.3 as void — no payload. The G2 has
  already shown the spec's type column can diverge from the wire (recording's
  "boolean" parameter takes payload `2` — `docs/ble/protocol.md` §6), so the
  actual write shape is an open question until sniffed/probed.
- **No inverse action.** Unlike record start/stop or a codec round trip,
  a still capture has no paired opposite action. This shapes the sniffer's
  window design (below) and means echo-based verification, if any exists,
  has no state to cross-check against yet.

---

## 2. The passive sniffer: `tools/sniffers/sniffer_photo.py`

Third consumer of the capture engine (`tools/common/capture.py`,
`docs/ble/sniffer_capture_engine.md`) — same connect → `run_capture_windows` →
`print_window_summary` → `save_capture` sequence as the recording and
settings sniffers, with `--actions`-overridable labels like the settings
sniffer.

### Default windows and their rationale

| Window | Operator does | Why it exists |
|---|---|---|
| `idle_baseline` | Nothing | Captures the ambient telemetry floor (categories `0x09`/`0x0C` tick ~1/s on the G2). Because photo capture has no paired opposite action, this window supplies the contrast `seed_triples_from_capture(exclude_ambient=True)` needs — with only photo windows, every window would contain the ambient triples and the filter would keep everything (`docs/ble/command_discovery.md`). |
| `photo_capture_1..3` | One photo each | Three separate single-photo windows make each capture unambiguously attributable and show whether a signal fires on *every* capture (genuine per-photo signal) or only the first (a one-time dump, like the connect-burst reports seen during settings work — `docs/ble/settings.md`). |

`idle_baseline` runs first so a slow after-effect of a capture (e.g. a
delayed storage update) cannot leak into the baseline.

### What to look for in the output

- A triple present in the photo windows but absent from `idle_baseline` —
  the photo-capture report candidate. `0x0A/0x03` would match the spec map,
  but take whatever the wire actually says.
- Category `0x09` movement: a photo consumes card space, so watch whether
  the 9.2 remaining-recording-time hypothesis signal (`docs/ble/protocol.md` §5)
  or anything else in category 9 ticks per photo. That would be the first
  concrete lead toward the remaining-photo-capacity state CLAUDE.md's
  storage gating (design principle 10) will eventually need.
- `CAMERA_STATUS` notifications during the photo windows — recording has no
  known status bit, but photo hasn't been checked at all.

### Known passive limit (precedent)

Some channels never report passively: `video_format` (`0x01/0x00`) never
appeared in any G2 notification across all settings captures
(`docs/ble/settings.md` §5). If every photo window dedupes to only the ambient
triples the idle window also shows, that is a *finding, not a failure* —
record it here, and move to active probing (§3). **This is exactly what the
2026-07-27 runs found, on both cameras — see §5.**

---

## 3. Active probe path (as planned; superseded by results — see §6-§7)

With the passive route exhausted (§5), the trigger needed to be probed
actively. There was no capture to seed `--from-capture` with — the seed
was manual, straight from the [spec] map. This section is kept as
written before any active attempt, for the reasoning trail; §6 and §7
record what actually happened.

1. **Void 10.3 first** (the spec's own typing) — `discover_command.py`
   sweeps a payloadless trigger via `--data-type VOID` (`generate_candidates`
   emits one candidate per reserved byte, `CandidateCommand.encode()` uses
   `encode_assign_void`, `protocol/codec.py`, added 2026-07-27).
   **Confirmed correct on the first genuinely-verified attempt — §7.**
2. **If void does nothing: INT8 payload sweep** on the same coordinates —
   the recording precedent (spec says "boolean", wire wanted int8 payload
   `2`) made a small-value int8 sweep the natural second hypothesis. Tried
   first in practice, before VOID support existed locally — §6.
3. **If both fail:** one further wire shape the tooling still cannot
   send — data-type byte `0x00` *with* a one-byte boolean payload (a real
   camera-report shape: the 2026-07-27 baselines caught `0x0C/0x04`
   reporting type `0x00` with payload `00`). **Moot** — void worked.
4. **Verification question:** still open — see §7's closing section.
5. **Profile shape:** one `commands.photo` block, same shape as
   `recording`, no `values` map since the trigger confirmed genuinely
   void — see §7 for the actual emitted block.

---

## 4. Open questions

- ~~Does a body-triggered still produce *any* INCOMING_CONTROL report?~~
  **Answered 2026-07-27: no, on both cameras (§5).**
- ~~Is the write void as the spec claims, or does it carry a payload like
  recording does?~~ **Answered 2026-07-27: genuinely void — confirmed on
  `POCKET_6K_G2 v7.9` (§7).**
- ~~Does an *accepted* BLE-written trigger echo, even though body-triggered
  stills don't report?~~ **Answered 2026-07-27: no — neither confirmed
  VOID write produced any coordinate-specific notification (§7), matching
  §5's passive result exactly.**
- ~~Does the reserved byte matter for this trigger?~~ **Answered
  2026-07-27: no — both `0x00` and `0x01` independently confirmed working
  (§7), unlike recording's exact-`0x01` requirement.**
- **Open, and now the blocking question:** what BLE-observable signal, if
  any, can confirm a photo was taken at runtime? The SD-card check that
  established §7's finding is not something controller code can perform.
  Candidates, all unconfirmed as a *reliable per-photo* signal: the
  `0x09/0x02` storage lead (§5.3 — fired once per ~3 photos in the
  passive baseline, far too coarse), a `CAMERA_STATUS` bit (never seen to
  move in a photo-specific way in any capture so far), or genuinely
  nothing over BLE. Blocks `CameraSession.capture_photo()` per design
  principle 3 until answered — see §7's closing discussion.
- What storage/state signal, if any, moves per photo? (`0x09/0x02` moved
  once per run in the passive baseline, not per photo — §5.3; not
  re-examined in the active runs.)
- Does photo capture require a particular camera state (e.g. not
  recording), and what does the camera report if it's refused (card full,
  no card)?
- Does the still button behave identically across codecs (a BRAW vs ProRes
  attribution session via `--actions` would answer this)? Still open —
  only tried at one codec/resolution so far.
- ~~Does the same `0x0A/0x03` VOID write work on `POCKET_6K_PRO v8.6`?~~
  **Answered 2026-07-27: yes — independently confirmed, same coordinates,
  same reserved-byte indifference, same SD-card verification method (§9).**

---

## 5. First captures — 2026-07-27, both cameras

One `sniffer_photo.py` run per camera, default windows (`idle_baseline`,
`photo_capture_1..3`), operator taking one still per photo window from the
body. G2 at BRAW/4K DCI/24fps; PRO at ProRes HQ/UHD/24fps (each camera's
connect-burst `recording_format` report confirms the starting state).
Capture JSONs: `tools/captures/POCKET_6K_G2_v7.9/…T104850.json`,
`tools/captures/POCKET_6K_PRO_v8.6/…T104533.json` (operator-side, gitignored
as always).

### 5.1 No photo-specific report exists (the headline result)

Across all six photo windows (three per camera), the only cleanly
attributable INCOMING_CONTROL triples were the known category-9 ambient
signals: `0x09/0x00` (the ~1/s ticker) in every window, and `0x09/0x02`
once per camera (§5.3). No `0x0A/0x03`, no new triple of any kind, and no
repeatable CAMERA_STATUS movement (§5.4). Photo capture is the first
command family whose body-triggered action is completely invisible on the
wire — stronger than the `video_format` precedent, where at least the
*consequences* of the change reported on other channels.

### 5.2 Methodological hazard confirmed: connect-burst contamination

Both cameras drain a large post-connect state dump over the indication
channel at a throttled ~180ms cadence lasting 10+ seconds (lens strings,
settings reports, `FF FF FF FF` marker packets, a `recording_format`
report, ...). Both `idle_baseline` windows were opened before the drain
finished and captured mid-burst packets instead of a clean ambient floor.
Worse, on the G2 the burst's tail — the ordered `0x0C 0x03`→`0x0F`
lens-string block ("Canon EF-S 18-55mm…", "f4.0", "26mm", …) — landed
*inside* `photo_capture_1`, where it initially looks exactly like a
photo-caused lens report. Three signatures give it away as burst drain, not
action response: the ~180ms spacing continuing unbroken across the window
boundary, the ascending parameter order, and the PRO's own connect dump
containing the same parameters. Mitigation now in the sniffer's docstring
and `docs/ble/sniffer_capture_engine.md`: open the first window only after
notifications slow to the ~1/s ambient cadence.

### 5.3 `0x09/0x02` — a genuinely useful storage lead, but not per-photo

New evidence for `docs/ble/protocol.md` §5's 9.2 remaining-recording-time
hypothesis: it fired **without any settings change** (the 2026-07-20
settings capture had only ever shown it after settings changes), exactly
once per run on *both* cameras — both times in `photo_capture_2` — and on
the PRO its moving int16 decreased from `11522` (connect burst) to `11521`
across three stills. That is consistent with "remaining recording time,
reported on change": three stills consumed roughly one unit of card space,
producing exactly one report somewhere in the sequence. Two consequences:
it is the first storage-capacity signal seen to move in response to photo
activity (relevant to design principle 10's remaining-photo state), and at
~one tick per several photos it is far too coarse to verify an individual
capture.

### 5.4 Singletons (recorded, not leads)

- One `CAMERA_STATUS` notify (`03`) on the G2, 0.2s after the connect
  burst finished draining — never repeated across all six photo windows on
  either camera. More plausibly the tail of the connect sequence than a
  photo response.
- The G2's `0x09/0x00` element 0 jumped regime (`0x1E7A` → `0x2F4A`)
  at the end of `photo_capture_1`, and its element 2 moved during the run
  (`0x19`→`0x1B`→`0x12`→`0x1F`) — contradicting the earlier "elements 1–2
  constant within a session" observation (element 1 stayed `100`
  everywhere; `docs/ble/protocol.md` §5's 9.0 row updated). Meaning still
  unknown; not photo-correlated in any repeatable way.

### 5.5 Data-type provenance side catch

These captures put three previously spec-only data-type bytes on the wire
for the first time (both cameras): `0x00` (void — payloadless `0x00/0x01`
one-shot-AF-coordinate reports — *and* boolean — `0x0C/0x04` with a single
payload byte), `0x05` (UTF-8 lens strings), and `0x80` (fixed16 — the G2's
`0x00/0x02` aperture report `0x2000`/2048 = AV 4.0 → f/4.0, exactly
matching the "f4.0" lens string in the same burst). `docs/ble/protocol.md` §3's
provenance list updated; int64 (`0x04`) is now the only official code never
observed on hardware.

---

## 6. First active probe — 2026-07-27, G2 — inconclusive

`discover_command.py --category 0x0A --parameter 0x03 --data-type INT8
--values 1,2,0 --reserved 0,1 --outcomes photo_taken` (6 candidates, the
INT8 fallback from §3, run instead of the VOID sweep — the operator's local
checkout predated the commit that added `--data-type VOID` support, and the
tool correctly rejected the seed with `--values` marked required at that
version; **pull the branch before the next run**). Capture:
`tools/captures/POCKET_6K_G2_v7.9/POCKET_6K_G2_v7.9_20260727T110927.json`.

### 6.1 What happened

The very first send (value=1, reserved=0) landed while the post-connect
burst was still draining — the operator correctly recognized the §5.2
signature (0x0C lens-string block at ~180ms spacing) and chose `[r]
repeat` rather than confirm on contaminated data. Good catch, and exactly
the discipline §5.2's mitigation calls for.

Every one of the following 6 confirmations — the repeat, then all 5
remaining candidates: values 1/2/0 crossed with reserved 0x00/0x01 — was
confirmed `photo_taken`. **No candidate was ever declined.** The tool's
`build_command_block` then correctly refused to emit anything: 6
confirmations under one outcome name, disagreeing on `value`/`reserved`, is
exactly the "a command block describes exactly one family" invariant it
exists to catch (`ValueError: Confirmed outcomes disagree on command
coordinates`). No data was lost — the capture JSON with all 6 windows'
wire evidence is intact — but no block was emitted, correctly, because six
different candidates can't share one outcome's payload slot.

**Tooling fix from this run:** the conflict was only surfaced at the very
end, after the camera session had already closed — costing the redo a full
reconnect. `discover_command.py`'s `probe_candidates` now warns immediately
after any confirmation that reuses an outcome name for a candidate with a
different `value`/`reserved` than an earlier confirmation of that same
outcome, printing both candidates and the same guidance below, so the
operator can course-correct while still standing at the camera instead of
finding out from a traceback afterward.

### 6.2 Why "confirmed every time" is not a finding yet

Two explanations fit the transcript, and the log alone cannot distinguish
them:

**(a) The confirmation itself was unreliable.** The operator had just
finished the §5 passive sniffer session, whose protocol requires manually
triggering a photo on the body during each window. A carried-over reflex
of "press the shutter, then say yes" — rather than judging whether *this
specific BLE write* caused the camera to act — would produce exactly this
transcript: uniform, unconditional confirmation regardless of value.

**(b) A genuine, value-insensitive trigger.** The spec types 10.3 as void
(§1) — if the firmware's actual behavior is "fire on any write reaching
this (category, parameter) coordinate, regardless of payload or even
declared data type," then every INT8 value tried, and presumably a true
VOID write too, would legitimately trigger a photo every time. This isn't
implausible: a firmware that expects a payloadless trigger has no
particular reason to validate or even read a payload someone else's INT8
write happens to attach.

The wire evidence doesn't cleanly separate these. The one piece of
independent-ish evidence — `0x09/0x02`, §5.3's remaining-capacity signal,
which fired once per ~3 real photos in the passive baseline — appeared
exactly **once** across all 6 supposedly-successful triggers here, where a
genuine 6-for-6 would predict roughly two occurrences. That's a mild lean
toward (a), but the baseline's own rate (1 per ~3) makes "1 occurrence in
6 trials" unsurprising under either hypothesis — not decisive either way.
**Do not write a `commands.photo` block, or update `commands.photo`'s
provenance to anything but absent, from this run.**

### 6.3 Redo protocol

1. **`git pull`** the branch first — the checkout that ran this predates
   both the VOID sweep support and the immediate-conflict warning (§6.1).
2. **Establish ground truth outside the BLE session.** Before sending
   anything, note the camera's own photo/clip count (its playback or media
   browser screen) — the same category of hazard the earlier lens-burst
   mistake (§5.2) revealed: don't trust a glance, check a real counter.
3. **One candidate per reconnect, or at minimum a hard pause between
   candidates** long enough to deliberately check that counter before
   answering the prompt — not "did I just see something," but "did the
   count go up."
4. **Include a negative control candidate** the operator is confident does
   nothing (e.g. a value already known inert on another parameter's
   coordinates spliced in mentally, or simply answering `[n] nothing` once
   deliberately as a sanity check on their own attentiveness) interleaved
   with the real sweep, to catch confirmation drift early rather than
   after 6 candidates.
5. **Try VOID first, per §3's ordering** — it's the spec's own typing and,
   per (b) above, the theoretically cleanest single test: if VOID alone
   reliably moves the counter and a clearly-wrong control does not, that's
   real evidence, in a way six same-outcome INT8 confirmations aren't.

---

## 7. Confirmed — 2026-07-27, G2 — VOID trigger, SD-card verified

Same-day redo of §6, following its own protocol almost exactly: pulled the
branch (picking up VOID sweep support), then ran the VOID candidate list
from §3:

```
python tools/control/discover_command.py \
    --model-key POCKET_6K_G2 --firmware v7.9 \
    --label photo --category 0x0A --parameter 0x03 --data-type VOID \
    --reserved 0,1 --outcomes photo_taken
```

The critical change from §6: **the operator verified ground truth by
inserting the SD card into a PC and inspecting its contents after each
send**, not by watching the camera or answering from impression. This is
strictly stronger evidence than anything else in this document so far —
stronger than an echo (which only proves the camera *acknowledged* a
write, not that a photo exists), stronger than an on-screen glance (§5.2
and §6 both show how unreliable that channel is here).

### 7.1 Result

Both candidates were sent and confirmed, each independently checked
against the SD card:

| Candidate | TX | Confirmed |
|---|---|---|
| reserved=`0x00` | `FF 04 00 00 0A 03 00 00` | photo_taken — new file on card |
| reserved=`0x01` | `FF 04 00 01 0A 03 00 00` | photo_taken — new file on card |

Both windows' wire capture shows only ambient telemetry (the first
candidate's window also caught the tail of the connect burst — the same
§5.2 signature, correctly not confused with a response) — **no
INCOMING_CONTROL notification specific to this write appeared either
time.** That's the expected result, not a gap: it's exactly what §5's
passive baseline already showed for body-triggered stills. A photo with
zero wire footprint is now established on both the passive and active
sides.

`build_command_block` correctly refused to emit a block from this run too
— same invariant as §6, one outcome can't hold two candidates — but this
time the "conflict" is a genuine positive result, not a reliability
problem: the reserved byte is confirmed **indifferent**, not ambiguous.
The `commands.photo` block was written directly into
`payloads/models/POCKET_6K_G2_v7.9.json` from this evidence (`reserved:
0`, chosen as this codebase's own default — `0x01` is equally confirmed
and noted in `provenance.notes`), rather than re-running hardware a third
time to force a single-candidate sweep the tool could emit unassisted.
`capabilities.supports_photo: true` was added to the same profile on the
strength of this result.

### 7.2 Why this one is trusted and §6 wasn't

§6's INT8 sweep confirmed every candidate too — on the surface, an
identical-looking pattern. The difference is the verification method, not
the outcome: §6's confirmations came from operator impression alone (the
exact channel already shown unreliable by the connect-burst mistake, §5.2,
and plausibly contaminated by reflexes carried over from the passive
sniffer protocol just run beforehand). §7's confirmations came from
physically inspecting the artifact the command is supposed to produce —
a channel with no plausible false-positive mechanism. "Every candidate
confirmed" is suspicious when the confirmation is a subjective read and
reassuring when it's an objective count, because in the latter case
uniform success is exactly what "the reserved byte doesn't matter"
predicts.

### 7.3 What's still missing

Confirming the trigger command is not the same as being able to verify a
write at runtime (CLAUDE.md design principle 3: "every write command must
be verified before reporting success"). The method that established §7.1
— pulling and inspecting the SD card — is not something `CameraSession`
can do from Python over BLE. As things stand, **no BLE channel confirms a
photo was taken**: not an echo (none exists for this coordinate, on either
the passive or active evidence), not `CAMERA_STATUS` (never seen to move
here), and the one storage lead that does exist (`0x09/0x02`, §5.3) is far
too coarse — it moves roughly once per three photos, not once per photo,
so it cannot distinguish "this specific write succeeded" from "some
unrelated write nearby also succeeded."

This is a genuine architectural question, not a small implementation
detail, and is left open rather than decided unilaterally here:

- Build `CameraSession.capture_photo()` now with only best-effort
  verification (e.g. cross-checking `0x09/0x02` when it happens to fire),
  clearly documented as not meeting the bar every other write in this
  codebase meets?
- Hold `protocol/categories/media.py` / `CameraSession.capture_photo()`
  until a real per-photo signal is found (e.g. deeper investigation of
  whether any category-9 or category-12 parameter moves reliably and
  promptly after a photo, the way `0x09/0x02` moves after a settings
  change)?
- Something else — e.g. an explicit, loudly-documented exception to
  principle 3 for this one command, with the caller told up front that
  "success" means "the write was sent," not "a photo was confirmed"?
- **TODO, operator-proposed 2026-07-27, not yet started:** verify out of
  band over USB instead of BLE. `POCKET_6K_PRO v8.6` exposes an HTTP
  interface over USB where clips/photos can be browsed and played back
  from a PC — a channel completely outside `INCOMING_CONTROL`/
  `CAMERA_STATUS`. **Explicitly noted by the operator as v8.6-only** — not
  claimed to exist or be usable on `POCKET_6K_G2 v7.9`; don't assume it
  transfers the way the trigger coordinates did (§9.1's caveat about
  independent-per-camera confirmation applies here too, if not more so,
  since this is a different subsystem entirely, not the same BLE
  protocol). Nothing has been investigated yet — no endpoint discovery,
  no confirmation this can distinguish "this specific photo" from "any
  recent photo," no design for how/whether `CameraSession` — which today
  only composes the BLE transport and BMD protocol layers, design
  principle 5 — would even compose with an out-of-band USB check. Picked
  up in a future session.

The protocol-level finding (§7.1) stands regardless of how this is
resolved — it belongs in the profile now, per design principle 6, whether
or not a session API is ever built on top of it.

---

## 8. Photo output dimensions and format — operator-provided, not wire-observed

Operator-reported (from camera experience/documentation, not a sniffer
capture or BLE signal — §7.1 already established the trigger carries no
payload and produces no wire report, so no BLE channel could reveal this
even in principle) on `POCKET_6K_G2 v7.9`:

- **BRAW:** a still's pixel dimensions equal the current recording
  *resolution* setting — quality/variant (Q0…5:1…) does not affect them.
- **ProRes:** a still's pixel dimensions are decided by *sensor area*
  instead, with only three possible readouts: 2.8K, 5.7K, and 6K.
- Both BRAW's resolution-driven dimensions and ProRes's three sensor-area
  dimensions are reported as **the same pixel counts**, and on this
  camera, every still, regardless of codec, is saved as **DNG**. **Not
  true on the PRO — see §8.4.**

### 8.1 Cross-check against the profile's `resolutions` table

All six of the BRAW dimensions given match `resolutions.*.width`/`height`
in `payloads/models/POCKET_6K_G2_v7.9.json` exactly (that table's own
values are themselves confirmed by the 2026-07-20 `dimension_enum` sweep,
`docs/ble/settings.md` §7):

| Name given | Dimensions given | Matches `resolutions` entry |
|---|---|---|
| 6K | 6144×3456 | `"6K 3:2"` |
| 6K 2.4:1 | 6144×2560 | `"6K 2.4:1"` |
| 5.7K 17:9 | 5744×3024 | `"5.7K 17:9"` |
| 4K DCI | 4096×2160 | `"4K DCI"` |
| 3.7K Anamorphic | 3728×3104 | `"3.7K Anamorphic"` |
| 2.8K 17:9 | 2868×1512 | `"2.8K 17:9"` |

⚠️ The `2.8K 17:9` row's `2868` is now disputed — both sniffed v8.6 profiles
report `2880` at that label (`docs/ble/settings.md` §18.1). That weakens this row
specifically, since the operator's figure and the v7.9 profile may trace to
the same source rather than being independent; the other five rows are
unaffected.

A clean 6-for-6 match is a strong, independent cross-check on that table
even though it comes from a completely different evidence channel (camera
output behavior, not a BLE write/report) — worth recording precisely
because it corroborates existing wire-verified data from an unrelated
direction.

**ProRes does not follow the same table**, and this is the important part
of the finding, not a footnote: the profile's `resolutions` entries for
ProRes are `"UHD"` (3840×2160) and `"HD"` (1920×1080) — the *video
recording* resolutions ProRes offers via `dimension_enum`. The operator's
report says ProRes *stills* ignore that entirely and instead take one of
three *sensor-area* readouts (2.8K/5.7K/6K) matching BRAW's own dimensions
for those names. **A still shot while the camera is set to ProRes/UHD is
not a scaled-down UHD-sized DNG** — it's a full-or-partial sensor-area
capture unrelated to the active video recording resolution. This directly
contradicts the naive assumption "photo dimensions = current video
resolution setting," which holds for BRAW but is wrong for ProRes — worth
stating explicitly since nothing else in this document would have
predicted the difference.

Disambiguated by the operator: ProRes's 6K sensor-area readout is plain
`"6K 3:2"` (6144×3456), not `"6K 2.4:1"` (6144×2560).

### 8.2 Why this doesn't become a profile/schema field

Nothing here is a BLE protocol value — there is no wire representation to
record, because §7.1 already confirmed the trigger is a bare void write
with no configurable payload, echo, or report of any kind. A hypothetical
`resolutions.*.photo_dimensions` schema field would encode a fact with no
corresponding write path (design principle 1 is about protocol values
specifically, but the same "don't invent structure ahead of a concrete
need" instinct applies here too) — so this stays prose in this doc rather
than schema until something in code actually needs to consume it (e.g. a
future `CameraSession` helper that predicts a still's expected dimensions
from the current recording state, if that's ever built).

### 8.3 What this changes for the still-open design questions

- **No BLE resolution parameter exists for stills, confirming §7.3's
  finding isn't accidental.** If dimensions were meant to be
  BLE-selectable per photo, the trigger would need a payload to carry that
  selection — it doesn't (confirmed void, both reserved bytes). Dimensions
  are apparently fully determined by camera state at the moment of
  capture, not by anything in the trigger command itself.
- **A future `capture_photo()` needs the caller already in the right
  state**, the same way `set_camera_format()` must be called *before*
  `capture_photo()` rather than the reverse — this is consistent with, not
  a change to, the existing `CameraSession` design (settings are
  orchestrated separately from the action that consumes them).
- **Design principle 10's storage/remaining-photo-capacity tracking will
  need this table anyway** eventually (remaining photo count depends on
  file size, which depends on dimensions) — a genuine future consumer for
  this data, just not today's.

### 8.4 File format differs by camera and codec — operator-provided

Correction to §8's opening claim, operator-reported: **"every still is
DNG" is a `POCKET_6K_G2 v7.9`-specific fact, not a general one.** On
`POCKET_6K_PRO v8.6`, the still's file format follows the active codec:

| Camera | Codec | Still format |
|---|---|---|
| `POCKET_6K_G2 v7.9` | BRAW | DNG |
| `POCKET_6K_G2 v7.9` | ProRes | DNG |
| `POCKET_6K_PRO v8.6` | BRAW | **`.braw`** |
| `POCKET_6K_PRO v8.6` | ProRes | DNG |

This is a genuine cross-model behavior difference, in the same vein as
§10.2's 5.7K/5.3K sensor-area difference — worth stating plainly rather
than letting a G2-derived assumption ("stills are always DNG") leak into
any future PRO-specific work. It also has no BLE angle at all: like every
other fact in §8, this is file-format behavior observed on the SD card,
not something the void, payload-less, echo-less trigger (§7.1) could ever
carry information about — nothing here changes §8.2's reasoning for
keeping this as prose rather than a schema field.

A `.braw` still is a real, if less obvious, consequence of §8's own
dimension finding: a BRAW still already inherits the *video* recording
resolution and, presumably, the BRAW *codec's own compression*, not a
generic bitmap conversion — a native `.braw` file for a BRAW-mode still
is the more natural fit than forcing it through a DNG (raw Bayer image)
container the way ProRes/DNG stills already are. Whether the G2 also
technically supports `.braw` stills and simply wasn't tested that way, or
whether the G2 firmware always converts to DNG regardless of active
codec, is not established — this section reports what was directly
observed on each camera, not what either camera is theoretically capable
of.

---

## 9. Confirmed — 2026-07-27, PRO — same VOID trigger, independently verified

Same-day repeat of §7's sweep on `POCKET_6K_PRO v8.6`:

```
python tools/control/discover_command.py \
    --model-key POCKET_6K_PRO --firmware v8.6 \
    --label photo --category 0x0A --parameter 0x03 --data-type VOID \
    --reserved 0,1 --outcomes photo_taken
```

Both candidates (`reserved=0x00`, `reserved=0x01`) confirmed `photo_taken`,
**each independently checked against the SD card's actual contents** —
same verification method as §7, confirmed explicitly by the operator
before this was trusted enough to write into the profile. Same
end-of-run `ValueError` from `build_command_block` for the same reason
(one outcome, two disagreeing candidates); same manual transcription into
the profile as a result, per §7.1's precedent, rather than forcing a
third hardware round to get a tool-emittable single-candidate sweep.

### 9.1 Result — identical finding, independently reached

`commands.photo` is now `VERIFIED` in
`payloads/models/POCKET_6K_PRO_v8.6.json` too: category `0x0A`, parameter
`0x03`, `VOID`, reserved indifferent (`0x00` canonical, `0x01` equally
confirmed), no echo or report of any kind in either capture window
(`tools/captures/POCKET_6K_PRO_v8.6/POCKET_6K_PRO_v8.6_20260727T140011.json`).
`capabilities.supports_photo: true` added to this profile alongside it.

This is deliberately **not** copied from the G2's entry — design
principle 6 requires independent confirmation per camera/firmware, and
this run supplied it: its own TX bytes, its own two capture windows, its
own SD-card check. That two cameras arrived at byte-identical coordinates
and byte-identical reserved-byte indifference independently is a genuine
cross-model data point (the still-capture command appears to be a fixed
part of the BMD BLE protocol, not something that varies per camera the
way the settings families' `dimension_enum`s do), but it's evidence, not
a substitute for the PRO's own confirmation.

One wire difference worth noting, not a discrepancy: the PRO's first
candidate window shows a longer/different-shaped connect-burst tail than
the G2's equivalent window (`0x01/0x10`, `0x04/0x07`, `0x0A/0x05`,
`0x09/0x08` appear here but not there) — consistent with this camera's
already-documented longer/heavier connect burst from the settings
investigation (`docs/ble/settings.md` §15's lens-burst timing note), not
anything specific to the photo trigger.

### 9.2 What this changes

Nothing about §7.3's open verification-strategy question — it now applies
identically to both cameras, which if anything strengthens the case for
resolving it once rather than per-camera: whatever answer is chosen
(best-effort signal, held API, or a documented exception) is very likely
to transfer across models the same way the trigger coordinates did,
since nothing camera-specific has shown up in this investigation so far.

---

## 10. Sniffing "Sensor Area" — `tools/sniffers/sniffer_sensor_area.py`

**First capture run 2026-07-27, G2 — result: real report activity, but no
directly-encoded sensor-area value found on either already-known channel.
See §10.1 for the full finding and §10.2 for the operator's cross-model
sensor-area matrix.** This was the natural next reverse-engineering step
§8 opened up: §8.1 established that ProRes stills follow a "sensor area"
concept (2.8K/5.7K/6K) with no relationship to
`resolutions.*.dimension_enums`'s ProRes entries (`UHD`/`HD`, the video
recording resolutions) — but that finding was operator-reported camera
behavior, not a BLE capture, so nothing was known about whether "Sensor
Area" has any BLE representation at all, or what it looks like on the
wire if it does.

The new sniffer follows the same pattern as `sniffer_photo.py` and
`sniffer_settings.py` (see `docs/ble/sniffer_capture_engine.md` — fourth
consumer): default windows are `idle_baseline` then one window per
concrete sensor-area value (`sensor_area_2_8k`, `sensor_area_5_7k`,
`sensor_area_6k`), operator-triggered on the body, `--actions`-overridable.
Precondition: the camera must already be in ProRes before running it —
BRAW stills follow the ordinary recording resolution instead (§8.1),
already fully modeled, nothing new to sniff there.

```
python tools/sniffers/sniffer_sensor_area.py
python tools/sniffers/sniffer_sensor_area.py --model-key POCKET_6K_PRO --firmware v8.6
```

### What the result would mean (as planned; see §10.1 for what happened)

- **If a category/parameter reports on the wire when Sensor Area
  changes:** seed `tools/control/discover_command.py --from-capture` with
  it and follow the standard discovery workflow (`docs/ble/command_discovery.md`)
  to find the write coordinates — this would become a new command family,
  `commands.sensor_area` or similar, structurally independent of
  `video_format`/`recording_format`. **Half right: reports appeared, but
  see §10.1 — their payloads didn't carry a sensor-area-specific value.**
- **If nothing reports** (matching the still-capture trigger's own null
  result, §5, and consistent with a menu setting that only affects local
  image-processing/readout without a corresponding BLE-visible state
  change): that's a real finding too, and would mean this codebase has no
  way to read or set Sensor Area over BLE at all — worth knowing before
  any future photo-capabilities work assumes it's controllable. **Not
  quite what happened either — something did report, just not something
  useful yet.**
- **A third possibility, not yet ruled out:** "Sensor Area" could turn out
  to just be a display name for something the `dimension_enum` sweep
  already touched (e.g. if a ProRes dimension_enum below the currently
  unexplained gap actually selects a sensor-area readout rather than a
  video resolution) — the capture would settle this by showing whether
  changing Sensor Area moves `recording_format`/`codec_quality`'s existing
  channels or something new entirely. **This is the closest of the
  three: it does move those exact channels — see §10.1.**

### 10.1 First capture result — 2026-07-27, G2

Default windows, camera pre-set to ProRes/Proxy/HD before the sensor-area
windows (the idle_baseline window still shows the connect-burst leftover
state, BRAW/Q5/6K 3:2 — the ProRes/HD switch happened between
idle_baseline and the first sensor-area window, off-wire, as the
operator's own menu setup). Capture:
`tools/captures/POCKET_6K_G2_v7.9/POCKET_6K_G2_v7.9_20260727T153744.json`.

**Real report activity, unlike the still-capture trigger.** Each of the
three sensor-area windows fired the same three-triple burst:
`recording_format` (`0x01/0x09`), `codec_quality` (`0x0A/0x00`), and the
`0x09/0x02` remaining-capacity-shaped signal (`docs/ble/protocol.md` §5, 9.2)
— on top of the ordinary `0x09/0x00` ambient ticker. This alone is a
genuine, useful finding: Sensor Area is not wire-silent the way the
still-capture trigger is.

**But neither of the two settings channels' payload actually encodes
which sensor area was chosen.** Decoding all three windows'
`recording_format` reports:

| Window | Payload (hex) | Decoded (fps, sensor_fps, width, height, flags) |
|---|---|---|
| `sensor_area_2_8k` | `18 00 18 00 80 07 38 04 13 00` | (24, 24, 1920, 1080, `0x0013`) |
| `sensor_area_5_7k` | `18 00 18 00 80 07 38 04 13 00` | (24, 24, 1920, 1080, `0x0013`) — byte-identical to 2.8K |
| `sensor_area_6k`   | `18 00 18 00 80 07 38 04 03 00` | (24, 24, 1920, 1080, `0x0003`) |

`width`/`height` decode to exactly `1920×1080` — the profile's `"HD"`
resolution — in **all three** windows, matching the ProRes/HD base the
operator set up before testing, not the chosen sensor area. Sensor area
is layered on top of a video resolution the camera picks independently
(consistent with the operator's own framing: "ProRes HD has 2.8K, 5.7K,
6K" as *options*, not resolutions in their own right) — but this channel
only reports the video resolution, never which option was picked.
`codec_quality` is the same story: all three windows report `codec_id=2
(ProRes), variant_id=3 (Proxy)`, byte-identical, matching the base codec
setup, not the sensor area.

**One partial, theory-consistent signal: the flags nibble.** `0x0013` for
2.8K and 5.7K vs `0x0003` for 6K — bit 4 (`0x10`) set only for the two
non-6K windows. This lines up exactly with the pre-existing "windowed
bit" hypothesis from the settings investigation (`docs/ble/settings.md`,
`docs/ble/protocol.md` §5 1.9): bit 4 clear = full-sensor/unwindowed readout,
bit 4 set = a windowed/cropped readout. `6K` here is presumably the
*full* sensor area (matching `"6K 3:2"`'s own established full-sensor
reading, `docs/ble/settings.md` §7), so its unwindowed flag is consistent;
2.8K and 5.7K are both smaller crops of the sensor, so both windowed is
also consistent. This is real corroborating evidence for the windowed-bit
theory, but it is **not** a 3-way encoding — 2.8K and 5.7K are
indistinguishable on this channel, both reporting `0x0013`. A binary
"is this the full sensor or not" bit cannot be the sole mechanism by
which the camera or this codebase could select a specific sensor area.

**A more promising, but single-sample, lead: `0x09/0x02`.** Its moving
int16 (payload offset 2, per `docs/ble/protocol.md` §5) took three genuinely
different values — 2.8K → `18620`, 5.7K → `4791`, 6K → `3928` — a
monotonic decrease as the sensor-area crop widens. This is consistent
with the existing 9.2 hypothesis ("remaining recording time,
bitrate-ordered": a wider sensor readout downsampled to the same HD
output plausibly costs more data/bitrate, leaving less "remaining"
capacity) — the *direction* makes physical sense, and it is the only one
of the three reporting channels whose value actually varies with sensor
area at all. But it is one sample per setting, from one session, and this
signal already free-runs on its own (`docs/ble/protocol.md` §5, 9.0/9.2) —
it needs a repeat/interleaved test (e.g. 2.8K → 6K → 2.8K, checking
whether the value returns to roughly the same reading) before trusting it
as a real per-setting correlate rather than coincidental drift.

**Bottom line:** this matches `docs/ble/settings.md` §7's "the report isn't
an ack, it's a state reflection" mechanistic insight, extended to a
parameter this codebase doesn't model at all — *some* settings change
(Sensor Area) triggered the camera to re-emit its currently-tracked state
on the channels it already reports on, without that state actually
containing the new setting's own value. No `commands.sensor_area` write
coordinates were found. No profile changes made from this capture — there
is nothing here that meets this codebase's evidentiary bar for even a
CANDIDATE entry (design principle 6).

**Next steps, ranked (updated after §10.3's, §10.4's, and §10.5's PRO
reruns):**

1. ~~Hunt for multiple `dimension_enum` values that all decode to the
   same `HD` width/height.~~ **Tried, PRO, §10.5: an apparent second enum
   (`0x00`) turned out to be a stale-state false positive, refuted by an
   immediate repeat run — only the already-known `0x03` reliably reaches
   `HD`.** Not fully exhausted (`0x17`-`0x1F` untried, per §10.5's closing
   options) but no longer the confident top lead it was before testing.
2. ~~A repeat/interleaved run to check whether `0x09/0x02`'s per-setting
   values are stable and reproducible, or just time-based drift.~~ **Done,
   PRO, §10.4: firmly negative — 0-for-2 independent PRO sensor-area
   sessions (8 windows total). Dropped from further priority; the G2 side
   of this question is now untestable on v7.9 hardware.**
3. `Operation.OFFSET` isolation-style probing (per the `docs/ble/settings.md`
   §16 precedent) or a wider `--listen-seconds` in case of a delayed
   report — full-channel decode of the existing captures is already done
   (nothing beyond the three-triple burst appeared in either camera's
   capture), so a genuine sensor-area-specific report, if the
   dimension_enum hypothesis above doesn't pan out, isn't in these
   captures at all and needs a different probe shape.
4. ~~Repeat on `POCKET_6K_PRO v8.6`~~ — done, §10.3.

### 10.2 Operator-provided: sensor-area options per model/video-resolution

Not wire-observed — camera-menu behavior reported directly by the
operator (2026-07-27), the same evidence category as §8's photo-dimension
notes:

| Camera | ProRes HD | ProRes UHD | ProRes 4K DCI |
|---|---|---|---|
| `POCKET_6K_G2 v7.9` | 2.8K, 5.7K, 6K | 5.7K, 6K | disabled |
| `POCKET_6K_PRO v8.6` | 2.8K, 5.3K, 6K | 5.3K, 6K | disabled |
| `POCKET_6K_G2 v8.6` | 2.8K, 5.3K, 6K | 5.7K, 6K | disabled |

Three things worth flagging:

- **The PRO's option set is genuinely different, not just relabeled** —
  `5.3K`, not `5.7K` — confirming design principle 6's stance is right
  even for operator-provided (non-wire) knowledge: nothing here should be
  assumed to transfer from the G2 to the PRO without its own check, and
  this table is the proof it doesn't always.
- **`POCKET_6K_G2 v8.6`'s row (operator-provided, 2026-07-31) is neither
  the v7.9 G2's nor the PRO's row, and mixes elements of both within a
  single camera.** HD's middle option is `5.3K` (matching the PRO's label,
  not v7.9 G2's own `5.7K`), but UHD's option set is `{5.7K, 6K}` — only
  two options, dropping `2.8K` entirely, and reusing the `5.7K` label HD
  doesn't use on this firmware. This is the first evidence in this table
  that the option set can differ **by resolution within one camera**, not
  just by camera — every earlier row assumed (from the data available at
  the time) that a camera offers one fixed option set reused at both HD
  and UHD. Confirms design principle 6 again, one level deeper: even a
  same-camera, same-firmware assumption ("HD's options apply to UHD too")
  doesn't hold without checking each resolution separately.
- **Disabled at ProRes/4K DCI on all three profiles** lines up with, but is
  not proven to be the same fact as, this codebase's own independently-found
  `resolutions."4K DCI".known_unreachable.ProRes` gap on all three profiles
  (`docs/ble/settings.md` §16, §7-§9, §18.12) — two different subsystems (video
  *recording* resolution selection vs. still-photo sensor-area selection)
  that happen to both go dark at exactly the same label, on every camera
  and firmware checked so far. Worth noting as a real coincidence-or-
  connection, not worth claiming as one and the same finding without more
  evidence — the video-resolution gap is a write-path failure this
  codebase's own commands hit, while the sensor-area disablement is a
  camera-body UI state the operator observed directly; either could
  explain the other, or they could be unrelated symptoms of the same
  underlying camera limitation (ProRes/4K DCI may simply not be a real,
  fully-supported combination on any of these cameras at all, only
  reachable through a body-menu quirk already documented elsewhere —
  `docs/ble/settings.md` §16's addendum). Left as an open observation.

### 10.3 Second capture — 2026-07-27, PRO — same negative result, one new cross-model reconfirmation

`sniffer_sensor_area.py --model-key POCKET_6K_PRO --firmware v8.6`, default
windows and labels, camera pre-set to ProRes/422/HD. Capture:
`tools/captures/POCKET_6K_PRO_v8.6/POCKET_6K_PRO_v8.6_20260727T155824.json`.

**Labeling caveat, worth flagging plainly:** the default `--actions` label
the operator did not override is `sensor_area_5_7k` — but §10.2's own
table says the PRO's real middle option is **5.3K**, not 5.7K. The
operator almost certainly selected the PRO's actual 5.3K option on the
body (there is no 5.7K choice to select on this camera), and the window
is simply mislabeled by the sniffer's G2-shaped default. This doesn't
change the finding below (the window's wire data turned out identical to
the 2.8K window regardless of which of the two it was), but it does mean
this run cannot be cited as "5.7K tested on the PRO" — it wasn't, and
can't have been. Fixed going forward: `sniffer_sensor_area.py`'s
docstring now calls out that the PRO's option set differs and to pass
explicit `--actions ...,sensor_area_5_3k,...` on that camera rather than
relying on the G2-shaped defaults.

**Same shape of result as the G2 (§10.1), with one channel missing.**
`recording_format` and `codec_quality` fired for every window, matching
§10.1 exactly — but **`0x09/0x02` did not fire at all**, in any of the
three windows, unlike its consistent one-per-window appearance on the G2.
This is a real negative data point against §10.1's "promising lead":
either the signal is genuinely intermittent/not-reliably-triggered by a
Sensor Area change (weakening it as a usable correlate), or this
particular run's `--listen-seconds` window simply closed before a delayed
report arrived. Either way, it did not reproduce here, so it should not
yet be treated as an established per-camera correlate — more testing
needed on both cameras before trusting it either way.

`recording_format` decoded across all three windows:

| Window | Payload tail (flags) | Decoded width/height | Flags |
|---|---|---|---|
| `sensor_area_2_8k` | `10 00` | 1920×1080 (HD) | `0x0010` |
| `sensor_area_5_7k` (see caveat above — actually 5.3K) | `10 00` | 1920×1080 (HD) — byte-identical to 2.8K | `0x0010` |
| `sensor_area_6k` | `00 00` | 1920×1080 (HD) | `0x0000` |

Exactly the G2's pattern, independently: width/height pinned to the
active video resolution (HD) regardless of sensor area; `codec_quality`
likewise pinned to `codec_id=2/variant_id=1` (ProRes/422) in all three
windows, matching the base setup, not the sensor area. **New cross-model
evidence: the windowed bit (`0x10`) is clear only for `6K` and set for
both smaller crops, on this camera too** — independently reproducing
§10.1's finding on a second camera with a different underlying flags
baseline (G2 showed `0x13`/`0x03`, carrying two extra low bits from a
different fps/M-rate combination; PRO shows a clean `0x10`/`0x00` — the
bit-4 boundary is what's common, not the exact byte value). This is now a
real cross-model reconfirmation of the windowed-bit hypothesis via the
sensor-area angle, on top of its original settings-investigation
provenance (`docs/ble/settings.md` §6-§7) — see `docs/ble/protocol.md` §5, 1.9.

**Bottom line, both cameras now captured:** no `commands.sensor_area`
write coordinates found on either camera. The only signal that
distinguishes sensor-area choices at all is the binary windowed bit
(full-sensor "6K" vs a smaller crop), confirmed independently on both
cameras — genuinely useful as corroboration for the existing
windowed-bit theory, but not a usable 3-way selector, and not something
this codebase can act on for reading/writing a specific sensor area. No
profile changes from either capture — nothing here clears design
principle 6's bar.

### 10.4 Interleaved repeat — 2026-07-27, PRO — windowed bit reproducibility CONFIRMED, capacity signal still absent

Following §10.1's "next steps" item 2 (checking `0x09/0x02` for
reproducibility vs. drift), but PRO-only: the operator's G2 was upgraded
to firmware v8.6 between sessions, so it can no longer be tested against
`payloads/models/POCKET_6K_G2_v7.9.json` — nothing new can be added to
§10.1's G2 evidence until a `POCKET_6K_G2_v8.6` profile exists someday.
Ran `sniffer_sensor_area.py --model-key POCKET_6K_PRO --firmware v8.6
--actions idle_baseline,sensor_area_2_8k,sensor_area_6k,sensor_area_2_8k,sensor_area_6k`
— an A-B-A-B interleave, 2.8K and 6K each set twice, with a longer pause
before closing each window than the single-pass runs used. Capture:
`tools/captures/POCKET_6K_PRO_v8.6/POCKET_6K_PRO_v8.6_20260727T162558.json`.

**Windowed bit: clean, byte-identical, reproducible toggle.**
`recording_format`'s flags decoded to exactly `0x0010` both times 2.8K
was selected and exactly `0x0000` both times 6K was selected — an A-B-A-B
pattern with zero deviation:

| Window | Flags |
|---|---|
| `sensor_area_2_8k` (1st) | `0x0010` |
| `sensor_area_6k` (1st) | `0x0000` |
| `sensor_area_2_8k` (2nd) | `0x0010` |
| `sensor_area_6k` (2nd) | `0x0000` |

This is meaningfully stronger evidence than §10.1/§10.3's single-pass
results: a signal that toggles cleanly on demand, twice each way, is
reproducible causation, not a one-off correlation. Width/height stayed
`1920×1080` and `codec_quality` stayed `ProRes/422` in all four windows,
same as every prior sensor-area capture — confirming again that neither
channel's *primary* payload carries the sensor-area choice, only this one
flags bit does.

**`0x09/0x02` still did not fire — not once, across all five windows,
despite longer per-window waits.** Combined with §10.3's single-pass PRO
result (also zero occurrences), this signal is now **0-for-2 independent
PRO sensor-area sessions (8 total sensor-area windows)**, a much firmer
negative than a single miss. Important nuance: this does not mean the
signal is dead on the PRO generally — it fired during the original PRO
photo-capture session (`docs/ble/photo_capture.md` §5.3, `photo_capture_2`
window) — only that it specifically does not respond to Sensor Area
changes on this camera, unlike its consistent one-per-window appearance
on the G2's single sensor-area sample. Given the G2 can no longer be
retested on v7.9, this asymmetry (fires for sensor area on G2, never for
sensor area on PRO, despite firing for other events on both) will likely
stay unresolved as either a genuine cross-model difference or an
artifact of the G2's single sample being an unlucky coincidence.

**Updated next-steps priority:** item 2 from §10.1 (the `0x09/0x02`
repeat test) is now done on the PRO with a firm negative result — drop it
from future priority. The dimension_enum-aliasing hunt (§10.1 item 1) is
unaffected and remains the top open lead, runnable on the PRO alone.

---

### 10.5 dimension_enum-aliasing hunt — 2026-07-27, PRO — apparent match REFUTED by a repeat run

§10.1 item 1's hypothesis, finally tested: is there a second
`dimension_enum` that decodes to the same `HD` width/height as the
already-known enum `3`, distinguishable by the windowed flags bit? Ran

```
python tools/control/sweep_dimension_enum.py \
    --model-key POCKET_6K_PRO --firmware v8.6 \
    --fps 24 --target-resolution "HD" --target-codec ProRes \
    --no-stop-on-match --include-known
```

**twice in immediate succession** (the operator's own repeat, not a
planned interleave — but it turned out to be exactly the check this
result needed). Captures:
`tools/captures/POCKET_6K_PRO_v8.6/POCKET_6K_PRO_v8.6_20260727T163737.json`
and `…T164102.json`.

#### First run: an apparent hit

Candidate `0x00` — never tried before, not in the profile — decoded to
`1920×1080` (HD) with `flags=0x0000` (unwindowed), while the already-known
`0x03` also matched at `1920×1080` with `flags=0x0010` (windowed). Two
enums, same resolution, different flags: exactly the signature §10.1's
hypothesis predicted for a genuine sensor-area-selecting enum pair.

#### Second run: contradiction

The identical command, run again right after, gave `0x00` a **completely
different** result: `6144×2560` ("6K 2.4:1"), `flags=0x0010` — not HD at
all. Every *other* candidate's result — including `0x03` and every
already-known enum (`0x06`/UHD, `0x08`/4K DCI-BRAW, `0x0D`/2.8K,
`0x0F`/3.7K Anamorphic, `0x12`/5.7K, `0x13`/6K, `0x14`/6K 2.4:1) —
reproduced byte-identically across both runs. Only `0x00` disagreed with
itself.

#### Diagnosis: `0x00` is a no-op; the "match" was stale leftover state

Cross-checking against what the camera held immediately *before* each
run's `0x00` send settles it. Run 1's `0x00` was the sweep's first
candidate, sent right after connect-settle — and the camera's prior
session was §10.4's interleaved sensor-area test, which ended at
ProRes/HD with the unwindowed ("6K" sensor area) flags. Run 1's `0x00`
result — `1920×1080`, `flags=0x0000` — is an exact match for that
leftover state, not a new one. Run 2's `0x00` was likewise this sweep's
first candidate, right after Run 1 had ended on candidate `0x14`
(`6144×2560`, "6K 2.4:1") with two silent candidates (`0x15`, `0x16`)
after it — so the camera was still sitting at "6K 2.4:1" when Run 2
started. Run 2's `0x00` result — `6144×2560`, `flags=0x0010` — is an
exact match for *that* leftover state.

**`0x00` never caused either reported state — both times, the "result"
was simply whatever the camera already held before the write, exactly
`docs/ble/settings.md` §7's "the report isn't an ack, it's a state
reflection" mechanism applied to an enum that (most likely) isn't a real,
assigned value at all.** This is a genuine false positive, the mirror
image of the false-negative lesson `CLAUDE.md`'s protocol table already
records for this same tool family (an `unconfirmed` result that turned
out to be a timeout artifact) — here, a `MATCH` turned out to be a
timing/state artifact instead. Both lessons say the same thing: a single
sweep's per-candidate result isn't self-certifying; check it against
context (a prior confirmed state, an independent repeat) before trusting
it.

#### Tooling fix: stale-match guard added to `sweep_dimension_enum.py`

`is_match` only ever checked a candidate's decoded state against the
*target* — it had no way to notice a match that was actually inherited
from before the write. The tool now tracks the last confirmed
`(width, height, flags)` state across candidates (carried forward through
silent ones) and flags any `MATCH` identical to it as a **possible stale
match**, inline during the sweep and in the final summary, with a
pointer to this finding. Real matches should come through clean unless
the immediately preceding candidate coincidentally reached the exact same
state — in which case the warning is itself useful signal to re-run from
a different starting point.

#### Bottom line: no second HD-aliasing enum found in 0x00–0x16

Only `0x03` — the already-known value — reliably, reproducibly reaches
`ProRes/HD`. §10.1 item 1's hypothesis is **not confirmed** within the
default sweep range. No profile changes from this result (a refuted
hypothesis isn't evidence for a `known_unreachable` entry either — it's
simply inconclusive within the range tested). Options going forward,
neither pursued yet:

- Extend the sweep to `0x17`–`0x1F` (the same second range the earlier
  ProRes/4K DCI hunt used, `docs/ble/settings.md` §16) for full 32-value
  coverage — worth doing for completeness, but tempered: every candidate
  in `0x00`–`0x16` other than the known values produced literally no
  report at all (a clean "invalid enum" signature, the same pattern the
  4K DCI hunt saw across its own untried range), which doesn't inspire
  confidence a second HD enum is hiding just past `0x16` either.
- Treat this as reasonably strong (not exhaustive) evidence that Sensor
  Area, at least at HD, is not selected via a second `video_format`
  `dimension_enum` — reopening the "does Sensor Area have any BLE
  representation at all" question §10.1's bottom line already left open,
  now with one more closed-off hypothesis.

---

### 10.6 Official spec search — no "Sensor Area" parameter exists anywhere

The operator located and directly searched the primary source: the
official *Blackmagic Camera Control* developer PDF
(`documents.blackmagicdesign.com/DeveloperManuals/BlackmagicCameraControl.pdf`,
115 pages) — every occurrence of "sensor" in the entire document, 26/26,
reviewed via in-PDF search. Two screenshots: category `10.0` (Codec) and
the `1.9` Recording Format struct, the latter showing every "sensor"
match in context.

**Result: no parameter named or resembling "Sensor Area" exists anywhere
in the spec.** Every "sensor"-prefixed term in the whole document belongs
to one place, `1.9` Recording Format:

- `[1] = sensor frame rate` — fps, valid only when sensor-off-speed is set
- `flags[1] = sensor-M-rate` — valid when sensor-off-speed is set
- `flags[2] = sensor-off-speed`
- `1.12` Shutter Speed's minimum value description also references
  "current sensor frame rate," the same concept, not a new one

All four are about *frame rate* (off-speed/slow-motion recording), not a
spatial crop or sensor-readout-region selection. The only genuinely
spatial concept anywhere near "sensor" in the whole document is `1.9`'s
own `flags[4] = windowed mode` — no "sensor" in its name, which is
exactly why the earlier `docs/ble/protocol.md` §3 provenance table (sourced
from a machine-readable transcription of this same spec, not this direct
search) already carried it as `windowed`, and why this codebase's own
independently-derived "windowed bit" hypothesis (built purely from wire
behavior — G2 settings work, then this whole §10 investigation) landed on
the *same* bit as the spec's own answer, without knowing that in advance.

#### What this settles

This closes the "search the spec for a Sensor Area parameter" avenue
definitively — not because the search was narrow, but because it wasn't:
every one of 26 hits for the exact word this investigation has been
chasing was checked, in the primary source, and none of them is it.
Combined with §10.1's sniffer captures (nothing new reports),
§10.5's dimension_enum hunt (no second enum found, `0x00`-`0x16`), and
this: **"windowed mode" — flags bit 4 of `1.9` Recording Format — is not
just this codebase's best available proxy signal for Sensor Area, it is
now confirmed to be the *only* officially-documented concept in the
entire protocol that has anything to do with sensor readout area.** If
Sensor Area has any further BLE representation beyond this single bit,
it is not in the official spec at all — it would have to be an
undocumented, vendor-extension parameter the way `0x09`'s write-margin
signal already is (`docs/ble/protocol.md` §5, category 9), found only by
further blind wire observation, not by reading the manual.

#### Why this still can't be a full 3-way selector

`windowed mode` is a single bit: on or off. Every capture so far (§10.1,
§10.3, §10.4) is consistent with exactly two BLE-visible states — full
sensor (`6K`, bit clear) and cropped (bit set) — never three. The
official spec confirms there is no *second* bit or field nearby that
could distinguish `2.8K` from `5.3K`/`5.7K` within the documented
protocol. Two readings of this, both worth keeping open:

- The camera's own UI genuinely offers three choices, but only two are
  distinguishable over BLE — the third dimension (which specific crop,
  not just cropped-or-not) may be a purely local, non-networked concept
  with no wire representation at all.
- Or a full encoding exists somewhere this investigation hasn't looked —
  but per the spec search above, it would have to be undocumented.

#### Recommendation (superseded — see §10.7)

Treat the Sensor Area BLE investigation as **effectively concluded** for
now: the windowed bit is the ceiling of what's discoverable through
spec-guided or passive/active wire investigation as currently scoped.
One concrete, not-yet-exhausted test remained: whether the windowed bit
itself is *writable*, independent of the 3-way selection question — see
§10.7 for that test and its result, which closes the investigation for
real.

---

### 10.7 Closing write test — 2026-07-27, PRO — windowed bit confirmed READ-ONLY

The one remaining concrete test from §10.6's recommendation: is
`recording_format`'s windowed bit independently *writable*, even if only
as a binary full-sensor/cropped toggle (not a 3-way Sensor Area
selector)? A single-variable isolation write — same `fps_int`/
`sensor_fps_int`/`width`/`height` as HD's already-confirmed values,
changing *only* the flags element — starting from a clean, known state
(Sensor Area set to `6K`/full-sensor on the body, `flags=0x00`):

```
python tools/control/send_settings_command.py \
    --model-key POCKET_6K_PRO --firmware v8.6 \
    --packet recording_format --raw-payload 24 24 1920 1080 16 \
    --listen-seconds 8
```

TX confirmed correct: `FF 0E 00 01 01 09 82 00 18 00 18 00 80 07 38 04 10
00` — `(24, 24, 1920, 1080, 0x10)`, exactly the intended single-bit flip
(`0x00` → `0x10`) with everything else unchanged. Capture:
`tools/captures/POCKET_6K_PRO_v8.6/POCKET_6K_PRO_v8.6_20260727T174908.json`.

**No echo.** Zero `0x01/0x09` reports over the full 8s window — only the
connect-burst lens-metadata tail (`0x0C`) and ambient `0x09/0x00`
telemetry. The same silent-write signature already established for
resolution retargets on this camera (`docs/ble/settings.md` §16).

**No physical effect either — the decisive part.** Per the test protocol
(previous turn), the operator took a photo immediately before this write
and another immediately after. **Both measured identical `6K` dimensions
on the SD card.** The write did not change what the camera actually
does, not just what it echoes.

This second check is what makes this result trustworthy in a way a
silent echo alone never is in this codebase: every other "no echo"
result elsewhere carries the standing caveat that a real change could
still have landed unconfirmed (the lens-burst delayed-echo pattern,
`docs/ble/session_and_verification.md`'s documented risk). Here that caveat
is closed off directly — there is independent, physical, SD-card ground
truth that nothing changed, not merely an absence of wire confirmation.

#### Conclusion

**The Sensor Area investigation is closed.** Full summary of everything
this section (§10) established:

- Passive sniffing found no dedicated report for Sensor Area changes on
  either camera (§10.1, §10.3) — only the pre-existing
  `recording_format`/`codec_quality`/ambient channels re-fire.
- An active `dimension_enum` hunt for a second enum aliasing to `HD`
  found nothing genuine — an apparent match was a stale-state false
  positive, refuted by an immediate repeat (§10.5).
- The full official 115-page spec document contains no parameter named
  or resembling "Sensor Area" anywhere (§10.6) — the closest and only
  related concept is `recording_format`'s own `windowed mode` flag bit.
- That bit is a real, reproducible **read** signal (§10.1/§10.3/§10.4:
  clear for full-sensor "6K", set for any smaller crop, toggles cleanly
  on demand) — but distinguishes only two states, never the full three,
  and is confirmed **not writable** via a direct isolated ASSIGN (§10.7,
  this section): no echo, and — decisively — no change in what the
  camera's own stored photos actually measure.

`commands.recording_format`'s provenance in
`payloads/models/POCKET_6K_PRO_v8.6.json` records this isolation test.
No other profile changes — there was never a `commands.sensor_area`
family to begin with, and nothing here creates evidence for one. Any
future revisit would need a fundamentally different approach (e.g. blind
full-channel monitoring across every category during a live change, the
way category 9's write-margin signal was originally found) rather than
another variation on what's already been tried here. Attention moves to
the photo-capture verification-strategy question (§7.3) and its USB TODO
— the higher-value open thread this whole detour grew out of.

**Third independent reconfirmation, 2026-07-31 (`POCKET_6K_G2 v8.6`,
`docs/ble/settings.md` §18.13).** §10.1's capture above was taken on
`POCKET_6K_G2 v7.9`, right before that unit's firmware upgrade — this
section's "either camera" always meant v7.9 G2 and `POCKET_6K_PRO v8.6`,
never v8.6 G2 specifically. A dedicated follow-up capture on `POCKET_6K_G2
v8.6` (motivated by an unrelated open discrepancy in that profile's
`recording_format.flags` field at UHD, not a revisit of this
investigation) independently reproduced the same read-only windowed-bit
correlation on this third (camera, firmware) pairing — full-sensor Sensor
Area reads `flags` bit 4 clear, any smaller crop reads it set, reproduced
2/2. This doesn't reopen anything above (no write attempt was made, no new
evidence about writability), but is worth noting as design principle 6 in
practice: the same conclusion re-earned on a new firmware rather than
assumed to carry over. `POCKET_6K_G2 v8.6`'s own `commands.recording_format`
provenance records the capture and its resolved discrepancy directly.


### 10.8 REST exposes Sensor Area as a readable field (2026-08-03)

§§10–10.7 closed the BLE search: no write path for Sensor Area exists on either camera,
by any means tried, and §10.6 confirmed no such parameter is documented anywhere in the
official 115-page spec. **All of that stands** — it is a finding about BLE, and the first
REST sweep does not contradict a word of it.

What the sweep adds is that the concept exists elsewhere. `GET /system/supportedFormats`
on `POCKET_6K_G2 v8.6` returns `sensorResolution` as a first-class field, and lists
ProRes at 1920×1080 **three times**, differing only by it:

| recordResolution | sensorResolution | Matches §10.2's option |
|---|---|---|
| 1920×1080 | 2880×1512 | 2.8K |
| 1920×1080 | 5376×3024 | 5.3K |
| 1920×1080 | 6144×3456 | 6K |

`GET /system/format` reports the active one (5744×3024 while the camera sat at ProRes/4K
DCI). So the selector §10.1–§10.5 could only infer from a single binary "windowed" flag
bit is directly readable over REST, with its actual dimensions.

Two things this does **not** establish, and must not be read as establishing:

- **Whether it is writable.** `PUT /system/format` has never been sent. §10.7's
  hard-won lesson — that a *read* signal existing says nothing about a *write* path, and
  that only before/after SD-card photo dimensions settled it — applies with full force
  here. The write probe is the next step.
- **Anything about `POCKET_6K_PRO v8.6`.** Not swept.

If the write does turn out to work, it resolves the gap §8 describes for ProRes stills
(which use sensor area rather than the video resolution) — over a transport this
investigation never had. See `docs/rest/transport.md`.

---

## 11. Phase 6 — `capture_photo()` built and confirmed over REST on real hardware (2026-08-04)

§7.3 left an open architectural question: how to reconcile "no BLE channel confirms a
photo was taken" with CLAUDE.md design principle 3's "every write command must be
verified before reporting success." Four options were on the table; this section records
which was chosen and what was built.

### 11.1 The chosen resolution: an explicit, loudly-documented exception

`CameraSession.capture_photo()` (`src/bmd_camera/ble/session.py`) exists now — it sends
the confirmed trigger (§7.1's `FF 04 00 00 0A 03 00 00`, via a new
`protocol/categories/media.py`) and nothing else. It never arms or waits on the
`NotificationRouter`, because there is nothing to wait on, and it never raises
`BMDVerificationError` — there is no timeout to fail. Its own docstring states this
explicitly, in the terms §7.3's third option proposed: this method's "success" means only
"the trigger was written to `OUTGOING_CONTROL`," never "a photo was confirmed taken." Real
confirmation is pushed entirely to the REST side (§11.2) — `CameraSession` stays BLE-only
(design principle 5) and never reaches into REST itself.

`capture_photo()` is capability-gated: it raises `BMDUnsupportedError` immediately unless
`profile.capabilities.get("supports_photo")` is `True`, and raises `ValueError` if the
profile has no `photo` command block at all (`POCKET_6K_G2 v8.6`'s current state — see
§11.4). This closed a real, separate gap while it was being touched: `capabilities` was
schema-validated (`payloads/ble_schema.json`) and populated (`supports_photo: true`,
§7.1/§9.1) but `CameraProfile` never actually *parsed* it into anything code could check —
design principle 7's capability model existed on paper for this field, not in practice.
Now it does (`docs/ble/payload_profiles.md`).

### 11.2 REST confirmation — `rest/media.py`

The out-of-band channel §7.3's TODO proposed, built now — but its design changed twice on
first real contact with hardware, both times documented here rather than quietly patched
over.

`examples/capture_photo.py` is the composition: one script holding a BLE `CameraSession`
and a REST `RestCameraSession` open to the same physical camera at once — the first thing
in this codebase to do that. The plan's own risk list flagged this combination as
untested ("concurrent BLE + REST is unverified... Phase 6 needs both open at once —
confirm on hardware").

**First real-hardware run, `POCKET_6K_PRO v8.6`, 2026-08-04:** got past `storage_state()`
(reported the active device correctly) and the then-existing clip-based prefix derivation
(derived `A001_08031748` from the newest clip) cleanly, then `mount_names()` raised
`BMDRestError: GET /mounts/ -> 404`. This was not a camera fact — it was a real
`RestClient` defect (`docs/rest/session.md`'s `list_mount()`/`mount_names()` section,
`docs/rest/transport.md`'s "Two URL namespaces on one host"): `get()` unconditionally
prepended `/control/api/v1` to every path, so the request went to
`/control/api/v1/mounts/` — a path that was never real; `/mounts/` is the Web Media
Manager, a separate namespace at the host root. Fixed by adding `api_prefixed: bool` to
`RestClient.get()`, with `mount_names()` passing `api_prefixed=False`.

**Second real-hardware run, same camera, same day, past the first fix:** this one ran all
the way through — storage check, mount resolution, the BLE trigger, the confirmation poll
— and printed "NOT confirmed — no new still appeared within 15.0s". The photo had, in
fact, been taken: pulling the SD card and opening `Stills\` in Windows Explorer showed
three real files, `A001_07311253_S001`, `A001_07311254_S002`, and — dated to the exact
minute of this run — `A001_08041126_S003`. This exposed a design defect, not a code bug.
The original design (`derive_still_prefix()`) assumed a still shares a clip's full
`<reel>_<date>` filename stem — an operator sample from the planning document, never
independently re-confirmed in this codebase (§11.3's original wording flagged exactly
this). The pulled card disproved it directly: only the leading reel identifier (`A001`)
is actually shared between a clip and a still. The middle timestamp segment is each
photo's *own* capture moment, generated fresh every time — one second apart between the
first two stills, three days apart on the third — and the trailing `_S<NNN>` counter is a
reel-wide cumulative count with no way to learn its current value in advance (Stills'
contents can never be listed — every subdirectory under a mount root `500`s
unconditionally, `docs/rest/transport.md`). A still's exact filename is therefore not
predictable or brute-forceable from clip data at all; probing for it, as the original
design did, was always going to find nothing.

`rest/media.py` was redesigned around a signal that needs no filename knowledge:
`stills_marker()` reads the Stills subdirectory's own `mtime` from the (working) mount
root listing — standard filesystem behaviour advances a directory's `mtime` whenever a
file is added inside it, without ever opening that directory. `wait_for_new_still()` now
polls for that `mtime` to change. `derive_still_prefix()`, `find_highest_still_index()`,
and the old filename-probing `wait_for_new_still()` were removed entirely, along with
`RestCameraSession.path_exists()` and `RestClient.exists()` (the binary-safe existence
probe they depended on), once nothing called them anymore.

**Third real-hardware run, same camera, same day, past the redesign:**
`examples/capture_photo.py` ran end to end and printed "Confirmed ✓" — the redesigned
`mtime` signal worked on its first try. Combined with the BLE trigger firing correctly in
all three runs (§11.1's TX bytes matched `FF 04 00 00 0A 03 00 00` exactly every time) and
the concurrent-BLE+REST combination working without incident, Phase 6's core confirmation
design is now real-hardware-confirmed.

**A follow-up request — "can I get the still name" — reopened the filename question, this
time as an explicit trade-off rather than a silent gap.** REST still cannot *guarantee* a
filename (the `500` above is permanent), but the real filenames observed on the pulled
card follow a knowable shape: `<reel>_<MMDDHHMM>_S<NNN><ext>`, where the reel and
timestamp are both derivable (the reel from the mount name already resolved; the
timestamp from the trigger's own send time, confirmed to land on the same minute twice
now) and only the counter is genuinely unknowable without a listing. `rest/media.py` grew
`guess_new_still_path()` — a deliberately **opt-in, informational-only** probe of a narrow
`(minute offset, index, extension)` window via the reintroduced `path_exists()`/
`RestClient.exists()` — that never gates `wait_for_new_still()`'s own pass/fail.
`examples/capture_photo.py` calls it once, after confirmation, purely to print a likely
name. This is new code as of this section's third hardware run and has not itself been
exercised on real hardware yet.

### 11.3 What's still genuinely unconfirmed

Being explicit about evidentiary weight, per this codebase's own discipline:

- **The trigger itself** (§7.1, §9.1) is real-hardware-confirmed, independently, on two
  cameras, and fired correctly in all three Phase 6 hardware runs (§11.2). Nothing about
  Phase 6 changes that.
- **Concurrent BLE + REST sessions** (§11.2) — confirmed working across the second and
  third runs: both sessions were open together, the BLE trigger fired, and the REST
  session polled (and, in the third run, confirmed) afterward without incident.
- **The redesigned `mtime`-based confirmation** (`stills_marker()`/`wait_for_new_still()`,
  §11.2) is now real-hardware-confirmed — the third run's "Confirmed ✓" is a genuine
  positive result, not just an absence of errors. One data point, not an exhaustive proof
  (repeated runs, a heavily-used card, a first-ever-photo card with no prior `Stills`
  directory are all still open questions), but the mechanism itself works.
- **`guess_new_still_path()`** (§11.2) is brand new — an opt-in, informational filename
  lookup built on the same real-hardware evidence as the confirmation redesign, but not
  itself yet run against a real camera. Whether its narrow default search window
  (`range(1, 11)` for the index) actually lands on the right filename in practice, versus
  needing a caller-supplied hint, is unconfirmed.
- **The mount-path resolution** deliberately avoids the one unconfirmed rule
  (`docs/rest/transport.md`'s `sd0`→`sd1` mapping, explicitly "not something to encode as
  a rule") by reading `GET /mounts/`'s own real listing instead — the single-mount case is
  now real-hardware-confirmed (all three hardware runs resolved `/mounts/A001-sd1/`
  correctly), but the *fallback* disambiguation-by-volume-prefix logic
  (`resolve_mount_path()`, for the case of more than one mount) still has no real-hardware
  test behind it, only unit tests against a fake.

None of this is a defect — it's the accurate confirmation status of a feature that was
wrong twice on first contact with real hardware, corrected both times, and confirmed
working on the third try, recorded honestly rather than glossed over, the same way every
other phase in this migration was.

### 11.4 `POCKET_6K_G2 v8.6` still needs its own trigger discovery

`capabilities.supports_photo` and `commands.photo` are confirmed only for
`POCKET_6K_G2 v7.9` and `POCKET_6K_PRO v8.6` (§7, §9) — not for `POCKET_6K_G2 v8.6`, this
codebase's usual primary reference. Design principle 6 forbids copying the v7.9 trigger
coordinates onto v8.6 without re-verifying: `tools/control/discover_command.py
--data-type VOID` needs to run against that specific firmware first, the same way every
other v8.6 command family was independently re-sniffed rather than inherited (e.g.
`docs/ble/recording.md`'s reserved-byte difference between v7.9 and v8.6). Until then,
`examples/capture_photo.py` defaults to `POCKET_6K_PRO v8.6`.
