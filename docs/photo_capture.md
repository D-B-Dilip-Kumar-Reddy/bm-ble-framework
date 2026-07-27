# Photo Capture

**Status:** the trigger command is confirmed on **both cameras**.
`commands.photo` (category `0x0A`, parameter `0x03`, `VOID`) is
`VERIFIED` as of 2026-07-27 in both `payloads/models/POCKET_6K_G2_v7.9.json`
(§7) and `payloads/models/POCKET_6K_PRO_v8.6.json` (§9), each
independently confirmed on its own hardware — a void ASSIGN to that
coordinate reliably fires a real photo capture on both, confirmed by
inspecting the SD card's contents on a PC after each send. What's still
missing, on both cameras: **no BLE-observable signal (echo or otherwise)
confirms a photo was taken** — every capture window around a
confirmed-successful trigger shows only ambient telemetry, matching the
passive finding (§5) that a body-triggered still produces no report
either. That leaves an open verification-strategy question (§7's closing
section, now applying identically to both cameras — §9.2) blocking
`protocol/categories/media.py`, `CameraSession.capture_photo()`, and
`examples/capture_photo.py` — all still planned (CLAUDE.md package
structure) — since CLAUDE.md design principle 3 requires every write to
be confirmed before reporting success, and no BLE channel currently does
that for this command. **TODO, not yet started:** the operator's proposed
path for verification is out-of-band, over USB rather than BLE —
`POCKET_6K_PRO v8.6` exposes clip/photo playback over an HTTP-over-USB
interface, explicitly noted as v8.6-only, not assumed to exist on the G2.
See §7.3's closing bullet. Picked up in a future session.

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
resolutions — and every still is DNG regardless of codec. §10 scaffolds
`tools/sniffers/sniffer_sensor_area.py` to find out whether that
sensor-area concept has any BLE representation at all — no capture run
yet.

Target camera for first bring-up: `POCKET_6K_G2 v7.9`, per CLAUDE.md's
camera registry ("start all new features with `POCKET_6K_G2 v7.9`").

---

## 1. Spec starting point

From the official SDI category tables (`docs/protocol.md` §5, all **[spec]**
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
  "boolean" parameter takes payload `2` — `docs/protocol.md` §6), so the
  actual write shape is an open question until sniffed/probed.
- **No inverse action.** Unlike record start/stop or a codec round trip,
  a still capture has no paired opposite action. This shapes the sniffer's
  window design (below) and means echo-based verification, if any exists,
  has no state to cross-check against yet.

---

## 2. The passive sniffer: `tools/sniffers/sniffer_photo.py`

Third consumer of the capture engine (`tools/common/capture.py`,
`docs/sniffer_capture_engine.md`) — same connect → `run_capture_windows` →
`print_window_summary` → `save_capture` sequence as the recording and
settings sniffers, with `--actions`-overridable labels like the settings
sniffer.

### Default windows and their rationale

| Window | Operator does | Why it exists |
|---|---|---|
| `idle_baseline` | Nothing | Captures the ambient telemetry floor (categories `0x09`/`0x0C` tick ~1/s on the G2). Because photo capture has no paired opposite action, this window supplies the contrast `seed_triples_from_capture(exclude_ambient=True)` needs — with only photo windows, every window would contain the ambient triples and the filter would keep everything (`docs/command_discovery.md`). |
| `photo_capture_1..3` | One photo each | Three separate single-photo windows make each capture unambiguously attributable and show whether a signal fires on *every* capture (genuine per-photo signal) or only the first (a one-time dump, like the connect-burst reports seen during settings work — `docs/settings.md`). |

`idle_baseline` runs first so a slow after-effect of a capture (e.g. a
delayed storage update) cannot leak into the baseline.

### What to look for in the output

- A triple present in the photo windows but absent from `idle_baseline` —
  the photo-capture report candidate. `0x0A/0x03` would match the spec map,
  but take whatever the wire actually says.
- Category `0x09` movement: a photo consumes card space, so watch whether
  the 9.2 remaining-recording-time hypothesis signal (`docs/protocol.md` §5)
  or anything else in category 9 ticks per photo. That would be the first
  concrete lead toward the remaining-photo-capacity state CLAUDE.md's
  storage gating (design principle 10) will eventually need.
- `CAMERA_STATUS` notifications during the photo windows — recording has no
  known status bit, but photo hasn't been checked at all.

### Known passive limit (precedent)

Some channels never report passively: `video_format` (`0x01/0x00`) never
appeared in any G2 notification across all settings captures
(`docs/settings.md` §5). If every photo window dedupes to only the ambient
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
and `docs/sniffer_capture_engine.md`: open the first window only after
notifications slow to the ~1/s ambient cadence.

### 5.3 `0x09/0x02` — a genuinely useful storage lead, but not per-photo

New evidence for `docs/protocol.md` §5's 9.2 remaining-recording-time
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
  everywhere; `docs/protocol.md` §5's 9.0 row updated). Meaning still
  unknown; not photo-correlated in any repeatable way.

### 5.5 Data-type provenance side catch

These captures put three previously spec-only data-type bytes on the wire
for the first time (both cameras): `0x00` (void — payloadless `0x00/0x01`
one-shot-AF-coordinate reports — *and* boolean — `0x0C/0x04` with a single
payload byte), `0x05` (UTF-8 lens strings), and `0x80` (fixed16 — the G2's
`0x00/0x02` aperture report `0x2000`/2048 = AV 4.0 → f/4.0, exactly
matching the "f4.0" lens string in the same burst). `docs/protocol.md` §3's
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
  dimensions are reported as **the same pixel counts**, and every still,
  regardless of codec, is saved as **DNG**.

### 8.1 Cross-check against the profile's `resolutions` table

All six of the BRAW dimensions given match `resolutions.*.width`/`height`
in `payloads/models/POCKET_6K_G2_v7.9.json` exactly (that table's own
values are themselves confirmed by the 2026-07-20 `dimension_enum` sweep,
`docs/settings.md` §7):

| Name given | Dimensions given | Matches `resolutions` entry |
|---|---|---|
| 6K | 6144×3456 | `"6K 3:2"` |
| 6K 2.4:1 | 6144×2560 | `"6K 2.4:1"` |
| 5.7K 17:9 | 5744×3024 | `"5.7K 17:9"` |
| 4K DCI | 4096×2160 | `"4K DCI"` |
| 3.7K Anamorphic | 3728×3104 | `"3.7K Anamorphic"` |
| 2.8K 17:9 | 2868×1512 | `"2.8K 17:9"` |

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
investigation (`docs/settings.md` §15's lens-burst timing note), not
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

Scaffold only — no capture has been run yet. This is the natural next
reverse-engineering step §8 opened up: §8.1 established that ProRes
stills follow a "sensor area" concept (2.8K/5.7K/6K) with no relationship
to `resolutions.*.dimension_enums`'s ProRes entries (`UHD`/`HD`, the
video recording resolutions) — but that finding was operator-reported
camera behavior, not a BLE capture, so nothing is known yet about whether
"Sensor Area" has any BLE representation at all, or what it looks like on
the wire if it does.

The new sniffer follows the same pattern as `sniffer_photo.py` and
`sniffer_settings.py` (see `docs/sniffer_capture_engine.md` — fourth
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

### What the result would mean

- **If a category/parameter reports on the wire when Sensor Area
  changes:** seed `tools/control/discover_command.py --from-capture` with
  it and follow the standard discovery workflow (`docs/command_discovery.md`)
  to find the write coordinates — this would become a new command family,
  `commands.sensor_area` or similar, structurally independent of
  `video_format`/`recording_format`.
- **If nothing reports** (matching the still-capture trigger's own null
  result, §5, and consistent with a menu setting that only affects local
  image-processing/readout without a corresponding BLE-visible state
  change): that's a real finding too, and would mean this codebase has no
  way to read or set Sensor Area over BLE at all — worth knowing before
  any future photo-capabilities work assumes it's controllable.
- **A third possibility, not yet ruled out:** "Sensor Area" could turn out
  to just be a display name for something the `dimension_enum` sweep
  already touched (e.g. if a ProRes dimension_enum below the currently
  unexplained gap actually selects a sensor-area readout rather than a
  video resolution) — the capture would settle this by showing whether
  changing Sensor Area moves `recording_format`/`codec_quality`'s existing
  channels or something new entirely.
