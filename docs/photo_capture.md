# Photo Capture

**Status:** passive phase complete — first real-hardware captures ran
2026-07-27 on **both** `POCKET_6K_G2 v7.9` and `POCKET_6K_PRO v8.6`
(`tools/sniffers/sniffer_photo.py`, default windows), with a decisive
negative result: **a body-triggered still produces no photo-specific report
on either camera** (§5). The next step is the active 10.3 void-trigger
probe, unblocked by `discover_command.py`'s VOID sweep support (§3). There
is still no `commands.photo` block in any profile, no
`protocol/categories/media.py`, no `CameraSession` photo API, and no
`examples/capture_photo.py` — all of those remain planned (CLAUDE.md
package structure).

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

## 3. Active probe path (current next step)

With the passive route exhausted (§5), the trigger must be probed actively.
There was no capture to seed `--from-capture` with — the seed is manual,
straight from the [spec] map.

1. **Void 10.3 first** (the spec's own typing). `discover_command.py` can
   now sweep a payloadless trigger — `generate_candidates` emits one
   candidate per reserved byte when the data type is VOID (no payload
   axis), `CandidateCommand.encode()` uses `encode_assign_void`
   (`protocol/codec.py`, added 2026-07-27 alongside these findings), and
   `--values`/`--restore-value` are rejected for a VOID sweep:

   ```
   python tools/control/discover_command.py \
       --model-key POCKET_6K_G2 --firmware v7.9 \
       --label photo --category 0x0A --parameter 0x03 --data-type VOID \
       --reserved 0,1 --outcomes photo_taken
   ```

   `--reserved 0,1` because the G2's recording command needed the
   non-default `0x01`. The operator watches the camera body — since §5
   shows stills produce no report, **operator confirmation is likely the
   only ground truth**; expect no echo even on success until proven
   otherwise.

2. **If void does nothing: INT8 payload sweep** on the same coordinates —
   the recording precedent (spec says "boolean", wire wanted int8 payload
   `2`) makes a small-value int8 sweep the natural second hypothesis:

   ```
   python tools/control/discover_command.py \
       --model-key POCKET_6K_G2 --firmware v7.9 \
       --label photo --category 0x0A --parameter 0x03 --data-type INT8 \
       --values 1,2,0 --reserved 0,1 --outcomes photo_taken
   ```

3. **If both fail:** one further wire shape exists that the tooling still
   cannot send — data-type byte `0x00` *with* a one-byte boolean payload
   (a real camera-report shape: the 2026-07-27 baselines caught
   `0x0C/0x04` reporting type `0x00` with payload `00`). Build that
   variant only if the first two sweeps are exhausted, per the same
   don't-build-speculatively rule that governed void support itself.

4. **Verification question (open):** what confirms a photo was taken?
   §5's finding makes this harder than recording: there is no passive
   report to model an echo on, and the only storage-side movement seen
   (`0x09/0x02`, §5) is too coarse to confirm an individual photo. Per
   design principle 3 the photo API cannot ship without an answer; per
   principle 10, storage preconditions gate the command — both need state
   sources that are themselves still undiscovered.

5. **Profile shape:** one `commands.photo` block, same shape as
   `recording`. If the trigger is confirmed void, the block carries no
   `values` map at all — the schema treats `values` as optional and
   `build_command_block` omits it for a VOID family.

---

## 4. Open questions

- ~~Does a body-triggered still produce *any* INCOMING_CONTROL report?~~
  **Answered 2026-07-27: no, on both cameras (§5).**
- Is the write void as the spec claims, or does it carry a payload like
  recording does? (§3's probe ladder — undetermined until an active sweep
  lands.)
- Does an *accepted* BLE-written trigger echo, even though body-triggered
  stills don't report? (Precedent either way: `codec_quality` echoes writes
  while body changes also report; `video_format` never reports *or* echoes
  a confirmation on its own channel.)
- What storage/state signal, if any, moves per photo? (`0x09/0x02` moved
  once per run, not per photo — §5.)
- Does photo capture require a particular camera state (e.g. not
  recording), and what does the camera report if it's refused (card full,
  no card)?
- Does the still button behave identically across codecs (a BRAW vs ProRes
  attribution session via `--actions` would answer this)?

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
