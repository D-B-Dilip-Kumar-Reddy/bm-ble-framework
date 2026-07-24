# Settings — codec, quality, resolution, FPS

**Status:** all three settings families are `VERIFIED` (2026-07-20).
`video_format` was promoted first (§8: 2/2 real `CameraSession` round
trips) and all 8 known `dimension_enum` values are confirmed by a clean,
decoded echo (§7). `codec_quality` and `recording_format` followed via
`set_camera_format` (§9), whose proxy path happened to produce the first
*genuine* (non-redundant) write+echo cycle for each — 1/1 each, §10.
That round also found and fixed a real bug in `set_video_format` (§10): a
codec-only switch (same resolution/fps) could spuriously fail because the
mode-notify channel doesn't encode codec, so a genuinely fresh report
looked like a stale duplicate — fixed by also watching the `codec_quality`
channel. A follow-up run then hit the *next* layer of the same underlying
issue: a redundant `set_codec_quality` call (requesting the value a
`video_format` switch had just reset quality to) reliably produces no
echo — now fixed for real via `last_known_codec_variant`, a
notification-derived no-op guard mirroring `record_stop()`'s (§11), rather
than just documented as a known risk. The same no-echo-on-redundant-write
behavior was then confirmed on real hardware for `video_format` and
`recording_format` too (§14, 2026-07-21: 7/7 and 5/5 `--repeat 2` runs)
and fixed the same way — `set_recording_format` and `set_video_format`
both now guard against it via notification-derived state, so all three
settings writes are hardened, not just `codec_quality`. 4K DCI/ProRes's
`dimension_enum` remains unknown after an exhaustive `0x01`–`0x16` search
(§7–§8), so `set_camera_format` (§9) reaches it with a two-step workaround
(proxy through UHD, then `recording_format`'s raw width/height) instead of
waiting on that gap to close. `_meta.status` stays `UNVERIFIED` overall
per design principle 8 — plenty of the profile is still unpopulated
(media, metadata, playback) even though every implemented settings family
is now verified.

**`POCKET_6K_PRO v8.6`** (§15, 2026-07-21) has all three command blocks and the
`codecs`/`resolutions`/`fps_modes` tables populated from its own captures —
`dimension_enum` and quality-variant values matching the G2's numbers exactly for
every one confirmed so far — but everything there is still `CANDIDATE`: no
`CameraSession` write+echo round trip has fully succeeded on this camera yet. §15
documents a PRO-specific finding: its on-screen display doesn't live-update after a
`video_format` write until the camera is power-cycled, even though the write
demonstrably takes effect. §16 (2026-07-22) documents a second, more serious
PRO-specific finding: `set_recording_format` cannot retarget resolution to 4K DCI while
ProRes is the active codec on this camera — confirmed 2/2 via real `CameraSession`
round trips — so unlike the G2, the two-step proxy workaround never actually reaches
ProRes/4K DCI here. A same-day addendum to §16 then confirmed via passive capture that
ProRes/4K DCI is nonetheless a real, representable state on this camera (reached by
hand through the body menu) — narrowing the gap to this codebase's write path rather
than a camera-side refusal, but not yet closing it. Two follow-up exhaustive
`dimension_enum` sweeps (`tools/control/sweep_dimension_enum.py`, `0x00`-`0x16` then
`0x17`-`0x1F` — 32 values total) then found no enum in either range reaches ProRes/4K
DCI — the same negative result the G2's own exhaustive search got, weakening the
"still-undiscovered enum nearby" hypothesis on both cameras enough that further blind
`dimension_enum` guessing is now a weaker lead than the alternatives (see §16). A
follow-up retry of `recording_format`'s retarget write with data-type byte `0x02`
instead of `0x82` (§16, 2026-07-23/24) also came back empty over a full 8s window —
ruling that hypothesis out too. `video_format`'s unexplained trailing elements,
probed via `--video-format-extra` (§16, 2026-07-24), fared no better — one pair
confirmed the override mechanism is safe but still landed UHD, three others were
silently rejected. All three original candidate hypotheses are now exhausted with
no match. A full-channel decode of the passive-capture evidence (§16, 2026-07-24)
then found nothing new either — no channel besides `recording_format` itself
correlates with the transition — leaving `Operation.OFFSET` (never tried, unlike
`ASSIGN` which every write above used) as the one remaining untested axis, now
testable via `--operation` (§16). An absolute-payload `OFFSET` test (§16,
2026-07-24) came back with zero response too, but per the spec's documented "add
to current value" semantics that's an unreliable test — the payload sent asked
for an out-of-range absolute width rather than a faithful delta. `--raw-payload`
(§16, 2026-07-24) then made a genuine in-range delta payload testable, bypassing
the profile's lookup tables — and it got the identical zero-response signature
(§16, 2026-07-24), which is stronger evidence than the absolute-payload result
since the delta landed exactly in-range. **Every hypothesis raised in this
investigation is now exhausted** with no confirming echo for the ProRes/4K DCI
retarget on this camera. This
blocks promoting the PRO's settings
families to `VERIFIED` until the combination is either fixed or explicitly excluded.

## Provenance and evidence status

The byte layouts and value tables below were originally transcribed from
`CODEC_RES_FPS_6K_G2.docx` (operator-supplied, 2026-07-20) — the write-up of
a reverse-engineering effort against a real `POCKET_6K_G2 v7.9`. That made
them, at the start, better than [spec] guesses but weaker than this repo's
[sniffer-verified] bar (CLAUDE.md design principle 6: values must originate
from a capture on that camera). They were therefore modeled with
`provenance.status: "CANDIDATE"` at first — every command family listed in
§1 has since been promoted to `VERIFIED` through this repo's own captures
and `CameraSession` round trips (§5–§11); see the top status line for where
each stands today. The operator's own original summary was explicit that
**not all packets are reverse-engineered** — known remaining gaps (chiefly
4K DCI/ProRes's `dimension_enum`) are listed per section below.

Key operator-reported findings this doc preserves:

1. **The codec_quality packet does NOT switch BRAW <-> ProRes.** It carries
   a codec id, but sending it with the other family's id changes nothing;
   only the quality variant within the active family changes.
2. **The video_format (FORMAT) packet is what switches the codec family**,
   via its `dimension_enum` byte, which encodes resolution and codec family
   together.

The verification runbook at the end of this doc exists to confirm or
falsify all of this with this repo's own captures — and to reverse-engineer
the same families on the other registry cameras.

---

## 1. The three packet families

All three are ordinary BMD command packets (`protocol/codec.py` framing:
`0xFF` prefix, length counting bytes 4+, ASSIGN operation). Encoders and
decoders live in `protocol/categories/settings.py`; every value below is
read from the profile, never hardcoded.

### 1.1 `codec_quality` — category `0x0A`, parameter `0x00`

```
FF 06 00 00 0A 00 01 00 03 03      BRAW 5:1
│  │  │  │  │  │  │  │  │  └─ variant_id (per-codec, see §2.1)
│  │  │  │  │  │  │  │  └─ codec_id (2 = ProRes, 3 = BRAW)
│  │  │  │  │  │  │  └─ operation: ASSIGN
│  │  │  │  │  │  └─ data type: 0x01 (INT8)
│  │  │  │  │  └─ parameter: 0
│  │  │  │  └─ category: 10 (Media)
│  │  │  └─ reserved: 0x00  (differs from recording/video_format's 0x01!)
│  │  └─ command id: 0
│  └─ length: 6 (bytes 4..9)
└─ fixed 0xFF prefix
```

Spec alignment: category 10 parameter 0 is the spec's **10.0 Codec**
(`int8 ×2`: basic codec + variant) — coordinates and element order match
exactly. The reported data-type byte is `0x01` (plain INT8), not an array
marker.

**Observed limitation (the doc's central caveat):** changes the quality
variant within the active codec family only. A BRAW->ProRes switch via this
packet does nothing — use `video_format`.

### 1.2 `video_format` (FORMAT) — category `0x01`, parameter `0x00`

```
FF 09 00 01 01 00 01 00 19 00 08 00 00      25 fps, 4K DCI, BRAW
│  │  │  │  │  │  │  │  │  │  │  └──┴─ two trailing 0x00 elements (see below)
│  │  │  │  │  │  │  │  │  │  └─ dimension_enum: resolution + codec family (§2.2)
│  │  │  │  │  │  │  │  │  └─ m_rate: 0 exact, 1 NTSC/drop
│  │  │  │  │  │  │  │  └─ fps_int as a plain byte (0x19 = 25)
│  │  │  │  │  │  │  └─ operation: ASSIGN
│  │  │  │  │  │  └─ data type: 0x01 (INT8)
│  │  │  │  │  └─ parameter: 0 — NOT 0x09
│  │  │  │  └─ category: 1 (Video)
│  │  │  └─ reserved: 0x01
│  │  └─ command id: 0
│  └─ length: 9 (bytes 4..12)
└─ fixed 0xFF prefix
```

Spec alignment: category 1 parameter 0 is the spec's **1.0 Video Mode**
(`int8 ×5`: frame rate, M-rate, dimensions, interlaced, colorspace). The
five elements line up perfectly, which gives the two "padding" bytes a
[hypothesis] reading: element 3 = interlaced (0), element 4 = colorspace
(0 = YUV). Until a capture shows either nonzero, they are encoded as
constant `0` and ignored on decode.

**This is the codec-switching packet**: `dimension_enum` selects resolution
AND codec family in one value (§2.2), so assigning a ProRes-locked enum
switches the camera to ProRes.

### 1.3 `recording_format` — category `0x01`, parameter `0x09`

```
FF 0E 00 01 01 09 82 00 | 19 00 | 19 00 | 00 10 | 70 08 | 10 00
│  │  │  │  │  │  │  │    │       │       │       │       └─ frame_flags int16 LE
│  │  │  │  │  │  │  │    │       │       │       └─ height (0x0870 = 2160)
│  │  │  │  │  │  │  │    │       │       └─ width  (0x1000 = 4096)
│  │  │  │  │  │  │  │    │       └─ sensor_fps_int (defaults to fps_int)
│  │  │  │  │  │  │  │    └─ fps_int (25)
│  │  │  │  │  │  │  └─ operation: ASSIGN
│  │  │  │  │  │  └─ data type: 0x82 — NOT official coding, see §3
│  │  │  │  │  └─ parameter: 9
│  │  │  │  └─ category: 1 (Video)
│  │  │  └─ reserved: 0x01
│  │  └─ length: 0x0E = 14 (bytes 4..17)
│  └─ command id: 0
└─ fixed 0xFF prefix
```

Spec alignment: category 1 parameter 9 is the spec's **1.9 Recording
Format** (`int16 ×5`: file frame rate, sensor frame rate, width, height,
flags) — coordinates and element order match exactly. (The source doc
described parameter `0x09` as a G2 deviation from "spec says 0x00"; the
official 1.9 assignment says otherwise — the deviation is not the
parameter, it's the data-type byte, §3.)

`frame_flags` observed values: `0x0010` (exact rate) and `0x0013`
(NTSC/drop) — narrower than the spec's five-bit flags description; only
these two observed values are modeled (design principle 6). Off-speed
sensor FPS has not been explored: `sensor_fps_int` always equals `fps_int`
in the source material.

---

## 2. Value tables (`POCKET_6K_G2 v7.9` — now VERIFIED via the command blocks that consume them)

Stored in `payloads/models/POCKET_6K_G2_v7.9.json` under `codecs`,
`resolutions`, and `fps_modes` (see `docs/payload_profiles.md` for the
structure). Never copy them to another model's profile — sniff that camera
(design principle 6).

### 2.1 Codecs and variants (`codecs`)

| Codec | `codec_id` | Variants (`variant_id`) |
|---|---|---|
| ProRes | 2 | HQ 0, 422 1, LT 2, Proxy 3 |
| BRAW | 3 | Q0 0, Q5 1, 3:1 2, 5:1 3, 8:1 4, 12:1 5, Q1 7, Q3 8 |

Variant ids are **per-codec** (BRAW 0 = Q0, ProRes 0 = HQ). BRAW variant id
6 is unobserved — a real gap between 12:1 (5) and Q1 (7); do not invent it.

### 2.2 Resolutions and dimension enums (`resolutions`)

Every row below is now **CONFIRMED**, not just transcribed: the 2026-07-20
`--dimension-enum` probe sweep (§7) sent each enum and decoded a clean
`0x01/0x09` report whose width/height matches exactly, plus a `0x0A/0x00`
report confirming the codec family.

| Label | Width × Height | Codecs offered | `dimension_enum` |
|---|---|---|---|
| HD | 1920 × 1080 | ProRes | ProRes: `0x03` ✅ |
| UHD | 3840 × 2160 | ProRes | ProRes: `0x06` ✅ |
| 4K DCI | 4096 × 2160 | BRAW, ProRes | BRAW: `0x08` ✅; **ProRes: still unknown** |
| 2.8K 17:9 | 2868 × 1512 | BRAW | BRAW: `0x0D` ✅ |
| 3.7K Anamorphic | 3728 × 3104 | BRAW | BRAW: `0x0F` ✅ |
| 5.7K 17:9 | 5744 × 3024 | BRAW | BRAW: `0x12` ✅ |
| 6K 3:2 | 6144 × 3456 | BRAW | BRAW: `0x13` ✅ |
| 6K 2.4:1 | 6144 × 2560 | BRAW | BRAW: `0x14` ✅ |

**Confirmed non-functional** (probed 2026-07-20, produced no state change —
see §7): `0x01`, `0x02`, `0x04`, `0x05`, `0x07`, `0x09`, `0x10`, `0x11`.
`0x10` in particular *disproves* an earlier hypothesis, below.

Known gaps:

- **4K DCI under ProRes remains unknown.** Every value in `0x01`–`0x14` was
  tried (§7); none produced it. Either its enum is in the untried range
  (`0x0A`, `0x0B`, `0x0C`, or anything `≥ 0x15`) or 4K DCI isn't reachable
  under ProRes via a single `dimension_enum` at all. Until found,
  `set_video_format("4K DCI", "ProRes", ...)` raises with a pointer here.
- ~~Two enums (`0x0F`/`0x10`) map to the same 3728×3104 dimensions~~
  **RESOLVED 2026-07-20, refuted**: `0x10` was probed directly and produced
  *no* change at all — it is not a second enum for this resolution, just an
  invalid value. The `resolutions` table's `"3.7K Anamorphic alt"` entry
  has been removed; `0x0F` is the only confirmed enum for 3728×3104.
- The enum space still has three untried values below `0x15` (`0x0A`,
  `0x0B`, `0x0C`) and nothing above `0x14` has been tried at all — more
  windowed/anamorphic/higher-resolution modes may live there.

### 2.3 FPS modes (`fps_modes`)

| Label | `fps_int` | `m_rate` (video_format) | `frame_flags` (recording_format) |
|---|---|---|---|
| 23.98 | 24 | 1 | `0x0013` |
| 24 | 24 | 0 | `0x0010` |
| 25 | 25 | 0 | `0x0010` |
| 29.97 | 30 | 1 | `0x0013` |
| 30 | 30 | 0 | `0x0010` |
| 50 | 50 | 0 | `0x0010` |
| 59.94 | 60 | 1 | `0x0013` |
| 60 | 60 | 0 | `0x0010` |

The NTSC labels (23.98/29.97/59.94) share `fps_int` with their exact
siblings and are distinguished only by `m_rate`/`frame_flags` — so a
decoded report of `fps_int=24` alone cannot distinguish 23.98 from 24.

**Caveat (from the 2026-07-20 capture, §5):** the `0x10` bit in
`frame_flags` appears to be the spec's resolution-dependent *windowed*
flag, not part of the fps encoding — the camera reported `0x0000` at
6K 3:2 (full sensor) at the same 50 fps that reports `0x0010` elsewhere.
The table's values hold as transcribed for windowed resolutions.

---

## 3. The `0x82` data-type byte (`DataType.INT16_ARRAY`)

The recording_format packet's data-type byte is `0x82` — not in the
official spec coding (`protocol/types.py`'s table stops at `FIXED16 =
0x80`). It is carried as `DataType.INT16_ARRAY = 0x82`, the same
"observed on the wire, absent from the public spec" precedent as
`Operation.CAMERA_REPORT` (`0x02`).

[hypothesis] `0x82 = 0x80 | 0x02` could read as "array flag + int16", but
`FIXED16` already occupies `0x80`, so the flag interpretation is
unconfirmed. Treat `0x82` as an opaque observed byte until more array-typed
parameters are captured.

`encode_assign_elements` (`protocol/codec.py`) is the multi-element sibling
of `encode_assign` that all three families use — it packs N same-typed
little-endian elements after the standard header.

---

## 4. Verification runbook

The point of this subsystem right now is to **turn CANDIDATE into VERIFIED
(or falsified) with this repo's own captures**. All tools log/save raw
bytes in the same uppercase-hex format as this doc, so captures diff
directly against §1's layouts.

### 4.1 Passive: confirm the families exist on the wire — ✅ DONE 2026-07-20

```bash
python tools/sniffers/sniffer_settings.py --model-key POCKET_6K_G2 --firmware v7.9
```

Default windows: `codec_to_prores`, `codec_to_braw`,
`quality_variant_change`, `resolution_change`, `fps_change` — the operator
performs each change on the camera body. **Run on real hardware
2026-07-20 — full results in §5.** Headline: `0x0A/0x00` and `0x01/0x09`
reports confirmed (operation `0x02`, payload shapes exactly as modeled);
`0x01/0x00` never reports, so **dimension enums cannot be captured
passively** — probe them actively instead:

```bash
# e.g. hunting the 4K DCI ProRes enum in the unobserved gap around 0x07/0x09
python tools/control/send_settings_command.py --model-key POCKET_6K_G2 --firmware v7.9 \
    --packet video_format --fps 25 --dimension-enum 0x09
```

One candidate per run, operator watching the body; the `0x01/0x09` report
in the saved capture shows the resulting width/height, which together with
the on-screen codec identifies what the enum selects. Add confirmed enums
to `resolutions.*.dimension_enums`.

For another model's tables (width/height/fps encodings, codec/variant
ids — the things the camera *does* report), use one sniffer window per
concrete setting:

```bash
python tools/sniffers/sniffer_settings.py --model-key POCKET_6K_PRO --firmware v8.6 \
    --actions res_HD,res_UHD,res_4K_DCI,codec_prores,codec_braw
```

### 4.2 Active: send each family and watch the camera — ✅ RUN 2026-07-20, results in §6

```bash
# The doc's central claim, run 1: codec_quality alone must NOT switch family
python tools/control/send_settings_command.py --model-key POCKET_6K_G2 --firmware v7.9 \
    --packet codec_quality --codec ProRes --variant HQ

# Run 2: video_format must switch it
python tools/control/send_settings_command.py --model-key POCKET_6K_G2 --firmware v7.9 \
    --packet video_format --resolution UHD --codec ProRes --fps 25

# Run 3: recording_format changes resolution/FPS within the family
python tools/control/send_settings_command.py --model-key POCKET_6K_G2 --firmware v7.9 \
    --packet recording_format --resolution "4K DCI" --fps 25
```

The tool shows the exact TX bytes and requires a typed `yes` (these are
CANDIDATE commands — the same safety stance as
`tools/control/discover_command.py`; `send_record_command.py` has no gate
because its family is VERIFIED). The operator's eyes on the camera are
ground truth; the saved capture is the evidence.

What to look for in each capture, now that §5 established the report
channels: a fresh `0x01/0x09` report whose width/height/fps match the
request, and (for codec changes) a `0x0A/0x00` report with the requested
ids. Two open questions §4.2 settles beyond the central claim: whether a
*written* `0x01/0x00` command gets a direct echo on its own coordinates
(body changes don't produce one), and whether the recording_format write is
accepted with data-type byte `0x82` (the camera's own reports use `0x02` —
if `0x82` is rejected, try the write with `0x02` and update the profile's
`data_type`). Also worth a run: `recording_format` targeting 6K 3:2 with
the transcribed windowed-bit flags vs `0x0000` (§5's frame_flags finding).

**Run this with a settled connection.** The first three real-hardware runs
(2026-07-20, §6) predate a fix: the tool wrote immediately after
connecting, with no wait for the camera's post-connect initial-payload
burst to drain, so all three captured that burst instead of a response to
the write. `--connect-settle-seconds` (default `6.0`s, added same date) now
waits it out first — every command above already benefits from it without
changing the command line.

### 4.3 End to end: the session path — ✅ RUN 2026-07-20, results in §8

```bash
python examples/change_codec.py
```

Runs `set_video_format` + `set_codec_quality` round trips (ProRes and back
to BRAW) through `CameraSession`'s echo verification. §5 established that
body-initiated changes report on `0x01/0x09` and `0x0A/0x00` — both
channels the session methods already await — so verification is *expected*
to work; whether a written command triggers the same reports is exactly
what this step tests. `set_video_format` passed 2/2, promoting
`commands.video_format` to `VERIFIED` — see §8 for the full byte evidence
and the (unrelated) reason `set_codec_quality` still needs a retest. A
`BMDVerificationError: no echo received` with the
camera *visibly changing* would mean writes don't trigger the reports body
changes do — feed that back into the profile blocks and, if needed, the
session verification strategy.

### 4.4 Promote (or falsify) the values

For each family confirmed on hardware: set its `provenance.status` to
`"VERIFIED"`, record the method/date/capture path, and fill in
`echo_operation`. If a claim falsifies (e.g. codec_quality *does* switch
family on some firmware), record that in the notes — the negative result is
protocol knowledge too. `_meta.status` stays `UNVERIFIED` until every
populated section is hardware-tested (design principle 8).

### 4.5 Other models

Repeat §4.1 with `--model-key POCKET_6K_PRO --firmware v8.6` (then the
URSAs when available — CLAUDE.md's registry expects different
category/param combos there). The PRO profile deliberately carries **no**
settings blocks or tables yet: nothing may be copied from the G2 (design
principle 6). The sniffer capture seeds either a manual transcription into
the profile or `tools/control/discover_command.py --from-capture` for
scalar sweeps; multi-element candidates are sent with
`send_settings_command.py` once transcribed.

---

## 5. First passive capture — results (2026-07-20, POCKET_6K_G2 v7.9)

Runbook §4.1, run by the operator on real hardware
(`tools/captures/POCKET_6K_G2_v7.9/POCKET_6K_G2_v7.9_20260720T103044.json`,
local/gitignored). Every window decoded cleanly. All claims below are
[sniffer-verified] for the **report** direction only — no command was sent.

### Confirmed

- **`0x0A/0x00` codec reports** — exactly the modeled int8 pair, operation
  `0x02`, data-type byte `0x01`, payload exactly 2 bytes:
  `02 00` (ProRes HQ), `03 02` (BRAW 3:1), `03 03` (BRAW 5:1). Confirms
  codec ids ProRes=2 / BRAW=3 and those three variant ids. The report fired
  in the codec, quality, and fps windows (retransmitted with unchanged
  values in the latter), but *not* in the resolution window.
- **`0x01/0x09` recording-format reports** — exactly the modeled five-int16
  element order, operation `0x02`, e.g.
  `32 00 32 00 00 10 70 08 10 00` = 50 fps, sensor 50, 4096×2160, flags
  `0x0010`, and `3C 00 3C 00 00 18 00 0A 13 00` = 60/59.94, 6144×2560,
  flags `0x0013`. Fired in **every** window — this is the camera's
  workhorse settings report.
- **`echo_operation = 2`** recorded in the profile for both families.
- **4K DCI under ProRes is real**: switching to ProRes on the body landed
  on 4096×2160 (its enum is still unknown, see below).

### Divergences from the transcribed model

- **The `0x01/0x09` report's data-type byte is `0x02` (plain `INT16`), not
  `0x82`.** `0x82` remains only the *claimed write* byte from the external
  doc — unverified until §4.2 runs. Decode is unaffected (same 2-byte
  element width); the profile block keeps `INT16_ARRAY` as the write byte
  with the discrepancy recorded in provenance.
- **`frame_flags` is not a pure fps encoding.** A third value, `0x0000`,
  was reported at 6K 3:2 (full sensor) @ 50 fps — same fps that reported
  `0x0010` at 4K DCI and 6K 2.4:1. The three observed values decode
  exactly as the official spec's 1.9 flags bitfield: bit 0 file-M-rate,
  bit 1 sensor-M-rate, bit 4 **windowed** (resolution-dependent — 6K 3:2
  is the G2's only full-sensor mode). `fps_modes.frame_flags` keeps the
  transcribed values (valid at windowed resolutions); a
  `recording_format` *write* targeting 6K 3:2 may need `0x0000` — test in
  §4.2 before trusting.

### The key negative result

**`0x01/0x00` (video_format/FORMAT) never reported — not even during
body-initiated BRAW↔ProRes switches.** The camera announces settings state
via `0x01/0x09` + `0x0A/0x00` only. Consequences, all folded into the code
and profile:

- Dimension enums are invisible to passive sniffing → the
  `--dimension-enum` probe mode on `send_settings_command.py` is the way to
  map missing enums (§4.1).
- A `video_format` write's confirmation must be watched on the other
  channels — validating `set_video_format`'s dual-channel arm (`1/0` is
  still armed in case a direct echo exists for *written* commands; body
  changes may simply not trigger it).

### Bonus observations (not yet modeled)

- **Shutter angle report [sniffer-verified]**: `FF 08 00 00 01 0B 03 02
  50 46 00 00` — category 1 parameter 11, int32 `18000` = 180.00°, exactly
  the spec's 1.11 (degrees × 100), emitted right after the fps change
  (shutter angle tracking fps). First int32 — and first `0x03` data-type
  byte — seen on the wire.
- **Category 9 parameter 2** fired once after *every* settings change; its
  int16 at payload offset 2 moved each time (2261 → 1852 → 3092 → 4156 →
  3473) in an order consistent with **remaining recording time at the new
  settings** (higher bitrate ⇒ smaller value: ProRes HQ 4K < BRAW 3:1 6K <
  5:1 < 2.4:1; adding 59.94 fps lowered it again). [hypothesis] only —
  needs a session with a different card fill level to confirm before it
  can enter the profile's `storage` section, but it's a promising lead for
  the planned storage-monitoring subsystem (remaining-time gating).
- The known category 9 parameter 0 ambient ticker ran throughout
  (`9x 2E 64 00 1F 00`, ~1/s), as in every other capture.

## 6. Second capture — active sends (2026-07-20, POCKET_6K_G2 v7.9)

Runbook §4.2, all three commands run by the operator on real hardware
(`tools/captures/POCKET_6K_G2_v7.9/POCKET_6K_G2_v7.9_20260720T150325.json`,
`..._20260720T150610.json`, `..._20260720T150742.json`, local/gitignored).
Unlike §5's passive run, these predate the `--connect-settle-seconds` fix
(below) — so this section splits cleanly into what the runs *did* confirm
(operator eyes, ground truth per the tool's own design stance) and what
they could *not* confirm (a clean echo, because the captured windows turned
out to be something else entirely).

### Operator-confirmed outcomes (real-hardware ground truth)

| Run | Command sent | TX bytes | Operator observation |
|---|---|---|---|
| 1 | `codec_quality` ProRes HQ (camera on BRAW) | `FF 06 00 00 0A 00 01 00 02 00` | **Codec did NOT change** |
| 2 | `video_format` UHD ProRes 25fps | `FF 09 00 01 01 00 01 00 19 00 06 00 00` | **Camera switched to ProRes @ UHD** |
| 3 | `recording_format` 4K DCI 25fps | `FF 0E 00 01 01 09 82 00 19 00 19 00 00 10 70 08 10 00` | **Resolution and frame rate both changed** |

This directly confirms, on real hardware, both of §1's central claims from
the external RE document: `codec_quality` alone cannot switch codec
families (run 1 — a ProRes id/variant sent while on BRAW did nothing), and
`video_format`'s `dimension_enum` is the packet that does (run 2). Run 3 is
the first positive evidence the `recording_format` write is accepted with
the claimed `0x82` (`INT16_ARRAY`) data-type byte specifically — the camera
didn't reject the packet, and both requested values took effect.

### The discovery: a tooling bug, not a protocol finding

None of the three captured response windows contain a report on the target
coordinates (`0x0A/0x00`, `0x01/0x00`/`0x01/0x09`, `0x01/0x09`
respectively). Instead each shows a burst of *unrelated* state — and a
different slice of it each time:

- Run 1: recording-state echo (`0x0A/0x01`), overlay enables (`0x03/0x00`),
  and category `0x0C` metadata (reel, scene tags, scene, take, good take,
  camera id, camera operator).
- Run 2: category `0x0C` lens metadata (lens type, iris, focal length,
  distance, filter, slate mode, an undocumented param `0x0F` reading
  `"Next Clip"`) and a `1/14` (ISO) report of `400`.
- Run 3: category `0` (Lens: aperture, zoom, AF trigger), `1/8`
  (sharpening), `1/2` (white balance), `3/3` (overlays), two undocumented
  category-9 ambient params, and one `9/1` (write-margin) report reading
  `low_margin` — see the caveat below.

This is exactly CLAUDE.md's documented "initial payload burst" — the flood
of state packets a just-connected camera sends before settling
(`docs/session_and_verification.md`'s `connect_settle_s`, chosen because
one prior capture saw it take **over 8 seconds** to fully drain). Each run
here reconnected and wrote within roughly a second of subscribing, with no
wait — so the tool's 3-second listen window sampled a different slice of
that multi-second burst each time, never long enough to also catch the
genuine response to the write. `send_settings_command.py` was the only
active-write tool in this repo missing the settle wait `CameraSession`
already has (see `docs/session_and_verification.md`); it's now fixed with
a `--connect-settle-seconds` flag (default `6.0`s, matching
`CameraSession`'s default) that waits after connecting, before the
send-and-capture window opens. `tools/control/discover_command.py` has the
same latent risk on its *first* candidate only — noted in
`docs/command_discovery.md`'s safety model, not yet fixed there since every
subsequent candidate is naturally protected by the prior candidate's
listen window.

**Net effect on provenance:** all three families stay `CANDIDATE` — the
operator-confirmed behavior is real evidence and is now recorded in each
block's `provenance.notes`, but the rigor bar this repo holds recording to
(a clean, decoded echo, repeated across cycles) hasn't been met for the
*write* side of any of the three yet. Re-run §4.2 with the fix, or run
§4.3 (`examples/change_codec.py`), which already goes through
`CameraSession.connect_settle_s` and was never affected by this bug.

### A one-off sighting, explicitly not treated as evidence

Run 3's window included a `write-margin warning` report
(`FF 07 00 00 09 01 01 02 00 FE 00`, `low_margin`/`-2`) — the same
CANDIDATE signal `docs/recording.md` correlates with a camera-initiated
recording stop on a slow SD card. Recording wasn't active here. Because
this run's whole window is the confounded initial burst rather than a
response to the resolution change, this single sighting is **not** used to
broaden that correlation to "precedes any settings change" — see
`docs/recording.md`'s write-margin section for the full reasoning. Worth
re-checking only if it recurs in a clean, post-settle capture.

## 7. Third capture — dimension_enum probe sweep (2026-07-20, POCKET_6K_G2 v7.9)

The runbook's answer to §2.2's biggest open question, run by the operator
with `send_settings_command.py --dimension-enum`
(`tools/captures/POCKET_6K_G2_v7.9/POCKET_6K_G2_v7.9_20260720T15{27,28,29,30,32,33,34,35,36,37,38,39,40}*.json`,
16 runs, local/gitignored). Unlike §6, these all ran **after** the
`--connect-settle-seconds` fix — every capture here is a clean response to
the write, not burst noise.

### Every existing table entry is now confirmed by a decoded echo

All 8 enums already in the `resolutions` table
(`0x03, 0x06, 0x08, 0x0D, 0x0F, 0x12, 0x13, 0x14`) were sent again here and
each produced a `0x01/0x09` report whose decoded width/height matches the
table exactly, plus a `0x0A/0x00` report confirming the codec family
(`02 00`/ProRes for the two ProRes enums, `03 03`/BRAW-5:1 for the six BRAW
ones — the quality variant carrying over from whatever `codec_quality` last
set, exactly as §6 established it should). Example — enum `0x13` (6K 3:2):

```
TX: FF 09 00 01 01 00 01 00 19 00 13 00 00
RX (0x01/0x09): 19 00 19 00 00 18 80 0D 00 00
    -> fps=25, sensor_fps=25, width=6144, height=3456, flags=0x0000
```

`6144×3456` matches `resolutions."6K 3:2"` exactly. The `flags=0x0000`
here — at 25fps, in a completely separate session from §6's 50fps sighting
— **independently reconfirms** §6's "windowed bit" finding: 6K 3:2 (the
G2's only full-sensor mode) reports `0x0000` regardless of frame rate,
while every other resolution here reports `0x0010`.

This is the strongest evidence any settings value has received: a decoded,
byte-exact echo *and* (per the operator's own summary of this round) a
confirmed physical camera change, for 8 independent resolution/codec
combinations in one sweep. It also settles the open question from §6:
**`0x01/0x00` still never echoes** — every one of these 16 sends confirmed
on `0x01/0x09` (and, for codec, `0x0A/0x00`) — the same channels
`CameraSession.set_video_format` was already arming.

### A mechanistic insight: the report isn't an ack, it's a state reflection

Eight further enums were tried and produced **no camera change** — the
`0x01/0x09` report in each of those captures decoded to whatever resolution
was already active (carried over from the previous successful send), not
an error or a distinct "rejected" signal:

| Enum tried | Result |
|---|---|
| `0x01`, `0x02`, `0x04`, `0x05`, `0x07`, `0x09`, `0x11` | No change — camera stayed on its prior resolution |
| `0x10` | No change — **refutes** the earlier "second enum for 3728×3104" hypothesis (§2.2) |

The camera evidently emits a `0x01/0x09` status reflection after **any**
`video_format` write, valid or not — reporting whatever the current state
actually is, not acknowledging the specific value it just received. A
sweep operator watching only the echo (not the physical camera) could be
fooled into thinking an invalid enum "did nothing but at least didn't
error" — which is true, but the report is not evidence the value was
understood, just that *something* is always reported back.

### Still open

4K DCI under ProRes remains unfound — every value `0x01`–`0x14` was tried
(§2.2's table plus this sweep) and none produced it. `0x0A`, `0x0B`,
`0x0C`, and everything `≥ 0x15` are still untried; one of those, or a
value this repo hasn't considered, is the next thing to probe.

### What this does — and doesn't — promote

`commands.video_format`'s `resolutions.*.dimension_enums` data now carries
this evidence in its `_comment`/`provenance.notes` (this round is data
confirmation, the same kind of update §5's passive capture made — not a
`provenance.status` promotion by itself). `commands.video_format.provenance.status`
stays `CANDIDATE`: this sweep used the raw `send_settings_command.py` tool,
not `CameraSession.set_video_format`'s own verification path — run
`examples/change_codec.py` for that before promoting to `VERIFIED`.

### A side finding for the write-margin signal

The write-margin warning (`storage.write_margin_warning`,
`docs/recording.md`) read `low_margin`/`-2` in essentially every one of
this sweep's ~18 connect cycles over roughly 15 minutes, with no recording
ever active. That's a much longer, more mundane persistence than the
signal's original evidence (a brief pre-stop warning). It doesn't disprove
the original correlation, but it does weaken "low_margin predicts an
imminent autostop" as a *general* reading — it may instead reflect a
per-(card, resolution/bitrate) threshold this specific SD card sits below
at the BRAW resolutions used here, unrelated to recording at all. Recorded
in the profile's provenance notes; no change to the signal's `values` or
`CameraSession`'s behavior.

## 8. Fourth round — CameraSession round trip + exhausted near-range enum search (2026-07-20)

Runbook §4.3, finally run: `examples/change_codec.py` against real
`POCKET_6K_G2 v7.9` hardware. This is the step every earlier round pointed
at as the remaining gap — up to now, every echo confirmation had come from
the raw `send_settings_command.py` tool, never from `CameraSession`'s own
verification logic.

### `set_video_format` — 2/2 confirmed through CameraSession itself

```
=== switch to ProRes (UHD @ 25) ===
TX: FF 09 00 01 01 00 01 00 19 00 06 00 00
RX (0x01/0x09): 19 00 19 00 00 0F 70 08 10 00  -> 3840x2160 = UHD
RX (0x0A/0x00, moments later): 02 00           -> ProRes, HQ
switch to ProRes (UHD @ 25) confirmed by echo ✓

=== switch back to BRAW (4K DCI @ 25) ===
TX: FF 09 00 01 01 00 01 00 19 00 08 00 00
RX (0x01/0x09): 19 00 19 00 00 10 70 08 10 00  -> 4096x2160 = 4K DCI
RX (0x0A/0x00, moments later): 03 03           -> BRAW, 5:1
switch back to BRAW (4K DCI @ 25) confirmed by echo ✓
```

`CameraSession.set_video_format()` armed both its own coordinates
(`0x01/0x00`) and the mode-notify coordinates (`0x01/0x09`) before each
write, exactly as designed since §6 — and, exactly as every prior round
predicted, the confirmation landed on `0x01/0x09` both times; `0x01/0x00`
itself still never echoed anything. **This promotes `commands.video_format`
to `VERIFIED`**: the packet bytes, the echo channel, and `CameraSession`'s
own arm/write/wait_for logic are now all confirmed on real hardware, not
just the raw tool. Combined with §7's 8/8 byte-exact `dimension_enum`
confirmations, this is the most thoroughly verified family in this doc.

A bonus confirmation rode along in the codec reports: after switching to
ProRes with no `codec_quality` write at all, the camera reported quality
`HQ` (`02 00`); after switching back to BRAW, it reported `5:1` (`03 03`)
— the same quality that had been active in BRAW before this run started
(from earlier probe-sweep sessions). See the next section for what this
implies.

### `set_codec_quality` — two false failures explain a real behavior

Both `set_codec_quality` calls in this run raised
`BMDVerificationError("no echo received")`:

```
=== set ProRes variant HQ ===
TX: FF 06 00 00 0A 00 01 00 02 00
(3s of only ambient category-9 ticks — no 0x0A/0x00 report)
set ProRes variant HQ NOT confirmed: ... no echo received within 3.0s

=== set BRAW variant 5:1 ===
TX: FF 06 00 00 0A 00 01 00 03 03
(3s of only ambient category-9 ticks — no 0x0A/0x00 report)
set BRAW variant 5:1 NOT confirmed: ... no echo received within 3.0s
```

These are **not** evidence `codec_quality` writes fail. Look at what each
one requested against what the *previous* section's codec report had just
shown: `set_codec_quality("ProRes", "HQ")` ran right after the camera had
already reported itself at ProRes/HQ; `set_codec_quality("BRAW", "5:1")`
ran right after it had reported BRAW/5:1. Both calls asked the camera to
do something it was already doing. The camera's `0x0A/0x00` report
evidently fires **only on an actual applied change** — a redundant write
gets no report and thus no echo, the exact same behavior
`docs/recording.md` documents for `record_stop()` (a redundant stop while
already stopped never echoes either). This is now documented on
`CameraSession.set_codec_quality`'s own docstring and in its
`BMDVerificationError` message.

This also reveals a mechanism worth naming: **a codec family remembers its
own last-set quality variant**, independent of the *other* family's
setting. Switching to ProRes doesn't reset quality to some fixed default —
it restores whatever ProRes was last set to (here, `HQ`, its apparent
factory/session default) — and switching back to BRAW restored `5:1`
(left over from §7's probe sweep, run in the same longer session). A
future write to one family's quality has no effect on the other family's
remembered value.

`examples/change_codec.py` had a latent bug exposed by this: it always
requested the same fixed target variant per family, which is guaranteed to
eventually collide with whatever that family's remembered value already
is (as it did here, on both legs). Fixed by sending two *different*
variants per family in sequence — the first may harmlessly no-op exactly
as above (not counted toward the script's pass/fail summary), the second
is guaranteed to be a real change from whatever preceded it, so it
actually exercises the write+echo path. `commands.codec_quality` stays
`CANDIDATE`: this run still produced zero real write+echo confirmations
for it (both were coincidental no-ops) — the fixed script is what would
finally produce one on a future run.

### 4K DCI/ProRes: the near-cluster search is exhausted

The operator additionally probed `0x0A`, `0x0B`, `0x0C`, `0x15`, and `0x16`
(not captured to a saved log this time) — none produced 4K DCI under
ProRes either. Combined with §7's `0x01`–`0x14` sweep, **every value from
`0x01` to `0x16` has now been tried**, and none selects it. Either its enum
lies well outside this cluster, or 4K DCI isn't reachable under ProRes via
a single `dimension_enum` byte at all. `resolutions."4K DCI".dimension_enums`
still has no `ProRes` entry, and `set_video_format("4K DCI", "ProRes", ...)`
still raises pointing here — this is now a settled, standing gap rather
than "not yet searched."

### The write-margin signal, once more

`low_margin`/`-2` appeared again immediately after the ProRes/UHD switch in
this run — a third corroboration (after §5's initial sighting and §7's
~18-cycle persistence) of the reading persisting across sessions,
resolutions, and now codec families, well outside any recording context.
No change to the signal's modeling; see `docs/recording.md`.

## 9. `set_camera_format` — the combination orchestration method

`CameraSession.set_camera_format(codec, variant, resolution, fps)` (added
2026-07-20) is the surface a script uses when it just wants "make the
camera look like this," without knowing which of the three settings
packets accomplishes which part, or that one combination needs a
workaround. Internally it sequences the three methods already documented
above:

1. **codec + resolution** — `set_video_format(resolution, codec, fps)`, if
   `resolution` has a known `dimension_enum` for `codec`. If not (today,
   only 4K DCI under ProRes — §7–§8's exhausted search), a `video_format`
   write can only select a (resolution, codec) pair it *has* an enum for,
   so it switches through the pixel-dimension-closest resolution that
   `codec` does have one for instead (`_closest_reachable_resolution`) —
   this only gets the codec **family** right, not yet the target
   resolution.
2. **quality** — `set_codec_quality(codec, variant)`, now that the codec
   family is confirmed active.
3. **fps (and, if step 1 took the proxy path, the real resolution)** —
   `set_recording_format(resolution, fps)`. This always targets the
   caller's real `resolution`, not the proxy: `recording_format` encodes
   raw width/height rather than a codec-locked enum, so it can retarget
   resolution within whatever family step 1 selected — confirmed on real
   hardware by §4.2's third run, which changed resolution+fps via
   `recording_format` alone with no `video_format` write in that request
   at all.

For 4K DCI/ProRes specifically this means: `video_format` to UHD/ProRes
(the closest ProRes-enabled resolution) → `codec_quality` to the requested
variant → `recording_format` to 4K DCI — two packets to get the codec
family right and the third to land the actual target, since no single
`dimension_enum` for that exact combination is known. For any combination
that *does* have a direct enum, step 1 already lands on the real target,
and step 3's `recording_format` call re-asserts the same values — redundant
in that case, but harmless, and keeps the method's behavior uniform
regardless of which path it took.

`_closest_reachable_resolution(target, codec)` picks by minimum pixel-count
distance (`|Δwidth| + |Δheight|`) among resolutions that have a
`dimension_enum` for `codec` — a principled, generalizable rule that
happens to select `UHD` for a `4K DCI` target today (the profile's only
other ProRes-enabled resolution), documented in the method's own
docstring. It raises `ValueError` if `codec` has no enum'd resolution at
all in the profile (nothing to proxy through).

**Adds no verification of its own** — each step already raises
`BMDVerificationError`/`BMDUnsupportedError`/`ValueError` independently,
and a failure at any step stops the sequence (later steps don't run).
Step 1's `video_format` write can reset quality to a value step 2 then
"sets" again — a real redundant write that the camera won't echo — but as
of §11, `set_codec_quality` recognizes this itself via
`last_known_codec_variant` and no-ops instead of raising, so
`set_camera_format` inherits that mitigation automatically; nothing
special needed here. `examples/change_codec.py` demonstrates both paths (a
direct BRAW/4K DCI combination and the ProRes/4K DCI proxy case) in one
run.

## 10. Fifth round — first genuine codec_quality/recording_format confirmations, and a real video_format bug (2026-07-20)

The rewritten `examples/change_codec.py` (§9) run against real
`POCKET_6K_G2 v7.9` hardware, calling
`set_camera_format("ProRes", "422", "4K DCI", "25")` then
`set_camera_format("BRAW", "5:1", "4K DCI", "25")`. The first call
succeeded outright; the second exposed a real bug in `set_video_format`.
Between them, this run promoted `codec_quality` and `recording_format` to
`VERIFIED`, joining `video_format` — every settings family this repo has
implemented so far is now hardware-verified.

### `codec_quality`: the first genuine write+echo cycle

`set_camera_format`'s proxy step had just switched to UHD/ProRes, which
reset the camera's remembered ProRes quality to `HQ` — and the very next
step asked for `422`, a **real** change for the first time in this doc's
history (every earlier attempt happened to request the value already
active). Result:

```
TX: FF 06 00 00 0A 00 01 00 02 01   (codec_id=2/ProRes, variant_id=1/422)
RX: FF 06 00 00 0A 00 01 02 02 01   (same pair, operation CAMERA_REPORT, ~200ms later)
```

Exact match. This is the confirmation every earlier round was missing —
`commands.codec_quality` moves to `VERIFIED`. Only one cycle so far
(`video_format` had two); a repeat isn't required to promote, matching how
`video_format` itself was promoted on real unambiguous evidence rather
than a fixed count, but would strengthen the record further.

### `recording_format`: the first genuine write with the real `0x82` byte

The same call's closing step sent `set_recording_format("4K DCI", "25")`
— the actual `0x82` (`INT16_ARRAY`) write byte, for the first time via
`CameraSession` itself (earlier "confirmations" were either operator-eyes
only on a burst-noise-confounded capture, §6, or the *result* of a
`video_format` write riding on this family's report channel, never a
`recording_format` write's own echo):

```
TX: FF 0E 00 01 01 09 82 00 19 00 19 00 00 10 70 08 10 00
RX: FF 0E 00 00 01 09 02 02 19 00 19 00 00 10 70 08 10 00   (~120ms later)
```

Both decode to `(fps=25, width=4096, height=2160, flags=0x0010)` — 4K DCI,
exact match, and the `0x82` byte was accepted without error.
`commands.recording_format` moves to `VERIFIED`.

### `video_format`: a real bug, found and fixed

The second `set_camera_format` call — same resolution and fps, only the
codec family changing (4K DCI/ProRes → 4K DCI/BRAW) — failed:

```
TX: FF 09 00 01 01 00 01 00 19 00 08 00 00
RX (1/9): FF 0E 00 00 01 09 02 02 19 00 19 00 00 10 70 08 10 00   <- REJECTED as a stale duplicate
RX (10/0, arrived 478ms after TX, well inside the timeout):
    FF 06 00 00 0A 00 01 02 03 03   <- BRAW, 5:1 — genuine confirmation, never even looked at
set BRAW 5:1 4K DCI @ 25 NOT confirmed: ... no echo received on any of [(1, 0), (1, 9)] ...
```

Root cause: the `0x01/0x09` mode-notify payload encodes fps/width/height
only, **never codec** — so when only the codec family changes, that
payload is byte-identical to what `NotificationRouter` already saw before
the write, and its stale-duplicate filter (correctly protecting other
families from real retransmit duplicates — see
`docs/session_and_verification.md`) discards the genuinely fresh report.
The camera's `0x0A/0x00` report (which the earlier §8 round already
observed follows *every* `video_format` write, not just ones paired with
an explicit `set_codec_quality` call) carried the real confirmation the
whole time — `set_video_format` just never watched that channel.

**Fixed**: `set_video_format` now arms and watches `codec_quality`'s
coordinates too (when the profile has that block), as a third
confirmation channel alongside its own `0x01/0x00` and the `0x01/0x09`
mode-notify. A codec-only switch always changes what that channel reports
even when it can't change `0x01/0x09`'s content. Verification there
compares only the reported `codec_id` (this method takes no `variant`
argument to compare against). This is a real correctness fix — the earlier
`video_format` `VERIFIED` promotion (§8) still stands (both of §8's runs
happened to also change resolution/fps, so `0x01/0x09` genuinely differed
and the bug never triggered there), but the fix closes a gap `VERIFIED`
should have covered from the start. See
`CameraSession.set_video_format`'s docstring for the full mechanism, and
`docs/session_and_verification.md` for why the router's duplicate filter
exists and isn't being weakened globally — this fix works around its one
known blind spot for one specific channel, not around the filter itself.

## 11. The redundant-write no-op fix (2026-07-20)

§10's fix made `set_video_format` reliably confirm a codec-only switch.
The very next `examples/change_codec.py` run (`set_camera_format("BRAW",
"5:1", "4K DCI", "25")`, right after a ProRes/422 leg) hit a *different*
failure at the *next* step — `set_codec_quality` — that looked identical
from the outside:

```
=== set BRAW 5:1 4K DCI @ 25 ===
TX (video_format): FF 09 00 01 01 00 01 00 19 00 08 00 00
RX (10/0, confirms video_format via §10's fix): FF 06 00 00 0A 00 01 02 03 03   -> BRAW, 5:1
TX (codec_quality): FF 06 00 00 0A 00 01 00 03 03   -> requests BRAW, 5:1 — ALREADY the state above
(3s of only ambient ticks — no 0x0A/0x00 report)
set BRAW 5:1 4K DCI @ 25 NOT confirmed: set_codec_quality(BRAW 5:1): no echo received ...
```

This is not a new bug — it's the *exact* documented false-positive from
§8/§10 (a `video_format` switch resets the family's remembered quality,
and requesting that same value again is a genuine no-op the camera never
echoes), just triggered predictably this time: `examples/change_codec.py`
always requests BRAW `5:1`, and `5:1` had become BRAW's remembered value
across earlier test sessions. The user's own read of the log ("the
settings is getting changed to BRAW 5:1 4K DCI @ 25, but the echo is
failing") was exactly right — the end state was correct, only the
verification was wrong.

**Fixed properly this time, not just documented.** `CameraSession` now
tracks `last_known_codec_variant: tuple[int, int] | None` — the
`(codec_id, variant_id)` from the most recent `codec_quality`-category
report, updated by a new `_observe_codec_quality` watcher wired into
`_handle_incoming` exactly like `is_recording`'s tracker (notification-
derived only, design principle 4 — never set from "we sent a command").
`set_codec_quality` checks it first: if `last_known_codec_variant` already
equals the requested `(codec_id, variant_id)`, the call returns
immediately — no write, no wait, no exception — mirroring `record_stop()`'s
`is_recording is False` early return (`docs/recording.md`) exactly.

This is real, not a heuristic: the guard only fires when a *prior
notification* (any of them — a body-initiated change, an earlier
`set_codec_quality` echo, or, as in this exact scenario, `set_video_format`'s
own confirmation landing on the `codec_quality` channel per §10's fix)
already proved the camera is at that state. The very first
`set_codec_quality` call in a fresh session, before any such report has
arrived, still writes and waits normally — there's nothing to skip on
yet.

Re-running the failing scenario with the fix (verified by direct
simulation, not yet re-run on hardware): given
`last_known_codec_variant == (3, 3)` (from the `video_format` step's own
confirmation), `set_camera_format("BRAW", "5:1", "4K DCI", "25")` now
completes without ever writing the redundant `codec_quality` packet —
matching what the camera was doing the whole time.

## 12. Code surface

| Piece | Where |
|---|---|
| Packet encoders/decoders | `protocol/categories/settings.py` (`encode_codec_quality`, `encode_video_format`, `encode_recording_format`, matching `decode_*`, `VideoFormat`/`RecordingFormat` dataclasses) |
| Multi-element ASSIGN encoder | `protocol/codec.py` `encode_assign_elements` |
| CANDIDATE data type | `protocol/types.py` `DataType.INT16_ARRAY` (0x82) |
| Profile blocks + tables | `payloads/models/POCKET_6K_G2_v7.9.json` `commands.codec_quality` / `commands.video_format` / `commands.recording_format`, `codecs`, `resolutions`, `fps_modes`; schema `$defs` `codecSpec`/`resolutionSpec`/`fpsModeSpec` |
| Profile accessors | `camera_profile.py` `require_codec` / `require_resolution` / `require_fps_mode` (`CodecSpec`/`ResolutionSpec`/`FpsModeSpec`) |
| Session methods | `session.py` `set_codec_quality` / `set_video_format` / `set_recording_format` (see `docs/session_and_verification.md` for the echo strategy), plus the `set_camera_format` orchestration (§9) and its `_closest_reachable_resolution` helper |
| Notification-derived state | `session.py` `last_known_codec_variant` + `_observe_codec_quality` (§11); `last_known_recording_format` + `_observe_recording_format` (§14) — feed the no-op guards in `set_codec_quality`, `set_recording_format`, and `set_video_format`, same discipline as `is_recording` |
| Tools | `tools/sniffers/sniffer_settings.py` (passive), `tools/control/send_settings_command.py` (active, typed-yes gated; `--repeat N` probes redundant-write echo behavior — §13, §14) |
| Example | `examples/change_codec.py` |
| Tests | `tests/unit/protocol/categories/test_settings.py`, plus settings cases in `test_codec.py`, `test_types.py`, `test_camera_profile.py`, `test_session.py` (`TestSetCameraFormat`, `TestClosestReachableResolution`, `TestObserveCodecQuality`) |

### Session verification strategy for settings writes

`set_codec_quality` and `set_recording_format` follow the standard
arm-write-await pattern on the command's own (category, parameter).
`set_video_format` arms **two** channels — its own `0x01/0x00` and the
recording_format coordinates `0x01/0x09` — and accepts the first fresh
report on either. §8 confirmed this design choice on real hardware: two
`CameraSession.set_video_format()` calls both landed their confirmation on
`0x01/0x09`, never `0x01/0x00`. Payload comparison per channel:

| Echo channel | Compared | Deliberately not compared |
|---|---|---|
| `codec_quality` own | `(codec_id, variant_id)` exact | — |
| `video_format` own | `(fps_int, m_rate, dimension_enum)` exact | trailing two elements (meaning unconfirmed) |
| `recording_format` (own, or as video_format's mode-notify) | `(fps_int, width, height)` exact | `sensor_fps_int`, `frame_flags` — the camera's own report of these hasn't been characterised; comparing them before that risks spurious verification failures |

A same-family codec_quality mismatch surfaces the documented
BRAW<->ProRes limitation in its error message. `BMDUnsupportedError` (first
use in this repo — design principle 7) is raised before any write when the
profile says the camera doesn't offer the codec at that resolution;
a missing dimension_enum raises `ValueError` pointing at the capture
workflow instead, since that's a profile gap, not a camera limitation.

**`codec_quality`'s "no echo" failure mode, confirmed on real hardware
(§8, §11):** the camera's `0x0A/0x00` report only fires on an *actual
applied change* — a `set_codec_quality` call requesting the (codec,
variant) the camera is already at (which happens easily right after
`set_video_format`, since a family remembers its own last-set quality
independently of the other family) produces no report at all. Originally
this surfaced as a `BMDVerificationError` indistinguishable from a real
failure; `set_codec_quality` now has an `is_recording`-style guard after
all — `last_known_codec_variant`, notification-derived (design principle
4) and updated from *any* codec_quality report regardless of source, lets
it recognize the target state as already-satisfied and return without
writing (§11). The guard only fires once a prior notification has proven
the state; before that (a session's very first `set_codec_quality` call)
it still writes and waits normally, and the original error message
remains for the case where it genuinely doesn't know.

**The same failure mode, confirmed and fixed for `video_format` and
`recording_format` too (§14):** real-hardware `--repeat 2` captures showed
both families share `codec_quality`'s exact silent-no-op behavior.
`set_recording_format` now has the same guard shape, via a new
`last_known_recording_format` field. `set_video_format` reuses that field
plus `last_known_codec_variant` together rather than tracking a third —
its guard fires only when both the codec family and the (fps_int, width,
height) are already confirmed by prior notifications.

---

## 13. Open question: does the same no-op-no-echo behavior apply to video_format and recording_format? (2026-07-21)

§11's `last_known_codec_variant` guard closes the redundant-write gap for
`set_codec_quality` specifically, because that family's no-echo-on-no-op
behavior was directly observed on real hardware. `set_camera_format`'s
other two steps can hit the structurally identical failure mode, but
**neither has been observed yet**:

- **`set_recording_format` (step 3)** requests `(resolution, fps)`
  directly. If step 1 (`set_video_format`, via its proxy path or
  otherwise) already landed the caller's exact target resolution, step 3's
  write asks for a state the camera is already in — possibly a redundant
  no-op with no echo, exactly like §11's `codec_quality` case.
- **`set_video_format` (step 1)** requests `(resolution, codec, fps)`
  together. If the camera is already in that exact combination, it's
  unknown whether any of its three watched channels (own, mode-notify,
  codec_quality) report anything at all.

Per CLAUDE.md's sniffer-first design principle, this is deliberately left
**unfixed** rather than speculatively guarded — a `last_known_*` guard
copy-pasted from §11 without a capture backing it would be exactly the
kind of invented protocol behavior that principle rules out. Instead,
`tools/control/send_settings_command.py` gained a `--repeat N` flag
(2026-07-21, see its module docstring and `docs/active_camera_control.md`)
that sends the same command twice in one session — send 1 lands the
target state, send 2 deliberately probes the redundant-write case — so
this can be answered with a real capture per family:

```
python tools/control/send_settings_command.py \
    --model-key POCKET_6K_G2 --firmware v7.9 \
    --packet recording_format --resolution "4K DCI" --fps 25 --repeat 2

python tools/control/send_settings_command.py \
    --model-key POCKET_6K_G2 --firmware v7.9 \
    --packet video_format --resolution UHD --codec ProRes --fps 25 --repeat 2
```

`(none observed)` on the second window reproduces §11's finding for that
family too, and the fix is the same shape: a notification-derived
`last_known_*` field plus an early-return guard in the matching
`CameraSession` method. A normal echo on both windows means that family
reports unconditionally and needs no guard at all. Until one of those runs
produces evidence, `set_camera_format`'s docstring correctly calls this a
known, unmitigated risk — that is accurate today, not a stale TODO.

---

## 14. The question answered, and all three families hardened (2026-07-21)

§13's open question is now closed with real-capture evidence, gathered the
same way §11's `codec_quality` finding was: `--repeat 2` runs against real
`POCKET_6K_G2 v7.9` hardware, covering every relevant category of starting
state relative to the target.

**`recording_format` — 5/5 runs confirm the same silent no-op.** Targeting
`--packet recording_format --resolution "4K DCI" --fps 25` from five
different prior states (including one already at the target), every run's
second (redundant) send produced zero `0x01/0x09` notifications — only the
ambient `0x09/0x00` storage telemetry. Send 1 always echoed normally,
tracking the *new* fps/width/height exactly (not the prior state), which
rules out the echo being unrelated connect-burst noise rather than a
genuine response.

**`video_format` — 7/7 runs confirm the same silent no-op.** Targeting
`--packet video_format --resolution UHD --codec ProRes --fps 25` across
same-family/different-resolution, exact-match-already, same-resolution/
different-fps, and full family+resolution switch starting states, every
run's second send produced zero notifications on *either* watched channel
(`0x01/0x09` mode-notify and `0x0A/0x00` codec_quality) — `video_format`'s
own channel (`0x01/0x00`) never appeared at all across any of the 14
windows, reconfirming §8's finding that it isn't a usable echo channel.

**`codec_quality` — 4 regression runs, consistent with §11, plus a fresh
family-limitation confirmation.** Two runs landed a genuine variant change
(422→HQ, LT→HQ) with a normal echo carrying the *new* variant id. One run
requested BRAW→ProRes while the camera was in BRAW — the report that came
back carried `(codec_id=3, variant_id=5)`, i.e. the *unchanged* BRAW/12:1
state, reconfirming codec_quality still can't switch families on its own
(§1); `set_codec_quality`'s existing mismatch-raising branch already
handles this correctly (reported tuple ≠ requested tuple → raises with the
documented family-limitation message), no change needed. The fourth run
(already at the exact target) is not independently conclusive on its own —
without a `--repeat` second send there's no way to rule out that single
report being ordinary connect-burst tail rather than a genuine echo — but
it doesn't contradict anything either, and §11's finding was already
established from genuine `CameraSession` production usage (the original
bug report this whole investigation started from), not from this tool.

**Hardening applied**, all in `src/bmd_ble/session.py`:

- A new notification-derived field, `last_known_recording_format:
  tuple[int, int, int] | None` (`fps_int, width, height` — deliberately
  excluding `sensor_fps_int`/`frame_flags`, matching what verification
  already compares), updated by a new `_observe_recording_format` method
  wired into `_handle_incoming` exactly like `_observe_codec_quality`.
  Because `set_video_format`'s mode-notify confirmation is the *same*
  (category, parameter) as `recording_format`'s own echo, this field
  updates from both — no separate video_format-specific tracking needed.
- `set_recording_format` gained the same no-op guard shape as
  `set_codec_quality`: early-return when `last_known_recording_format`
  already equals `(fps_int, width, height)` for the request.
- `set_video_format` gained a no-op guard that reuses *both* existing
  fields rather than tracking a third: early-return when
  `last_known_codec_variant`'s codec id already matches the requested
  codec **and** `last_known_recording_format` already matches the
  requested `(fps_int, width, height)` — the two together are exactly
  video_format's full observable state (codec family + resolution + fps).
- `set_camera_format`'s docstring no longer calls the redundant-follow-up
  risk "not papered over" — all three steps now self-guard.

See §12's code surface table for the updated field/method list.

---

## 15. POCKET_6K_PRO v8.6 — dimension_enum sweep and the on-screen UI staleness finding (2026-07-21)

Following the reverse-engineering procedure in `CLAUDE.md` (Phase 3), the PRO's own
passive captures confirmed `codec_quality` (`0x0A/0x00`) and `recording_format`
(`0x01/0x09`) report with the *exact same category/parameter and payload shape* as the
G2 — a real, independently-confirmed data point (not copied), and the basis for trying
`video_format`'s coordinates (`0x01/0x00`) unchanged too, since it never reports
passively on either camera (§8's finding held here as well).

**The dimension_enum sweep.** `tools/control/send_settings_command.py --packet
video_format --dimension-enum 0x..` was swept across candidates on real
`POCKET_6K_PRO v8.6` hardware. Every candidate below produced a genuine, repeatable
transition on the `recording_format` mode-notify channel (`0x0F` was operator-verified
directly rather than decoded from a pasted capture, but follows the identical
enum-matches-the-G2 pattern), and — critically — every enum value matches the G2's own
number for the same resolution:

| Enum | Resolution | Codec | Pixel dimensions |
|---|---|---|---|
| `0x03` | HD | ProRes | 1920×1080 |
| `0x06` | UHD | ProRes | 3840×2160 |
| `0x08` | 4K DCI | **BRAW only** | 4096×2160 |
| `0x0D` | 2.8K 17:9 | BRAW | 2880×1512 |
| `0x0F` | 3.7K Anamorphic | BRAW | 3728×3104 |
| `0x12` | 5.7K 17:9 | BRAW | 5744×3024 |
| `0x13` | 6K | BRAW | 6144×3456 |
| `0x14` | 6K 2.4:1 | BRAW | 6144×2560 |

Codec family per resolution came from the operator, confirmed against the same
HD/UHD-under-ProRes, everything-else-under-BRAW split the G2 has, with the same
4K DCI carve-out: the camera offers 4K DCI under *both* codecs, but the only known
`dimension_enum` (`0x08`) reaches BRAW's 4K DCI — ProRes's 4K DCI enum is an
open gap here too, unresolved on the G2 despite an exhaustive `0x01`–`0x16` search
(§7–§8) and not yet exhaustively searched on the PRO either.

**Unresolved: `0x02` vs `0x13` for "6K".** `0x02` also produced a transition to
6144×3456 once, but clamped the requested 50fps down to 30 and did not repeat on a
later attempt (no confirming echo that time — could be a genuine redundant-write no-op,
or the candidate may simply not be reliable). `0x13` reproduced 6144×3456 cleanly at
the requested 50fps on a separate occasion. The profile records only `0x13` for
`resolutions."6K".dimension_enums.BRAW`, per the schema's one-enum-per-pair
constraint; `0x02`'s behavior is noted in `commands.video_format`'s provenance for
whoever investigates it further. `frame_flags` reported `0x00` for both attempts at
6144×3456 (full-sensor readout) versus `0x10` for every other resolution
(windowed/cropped) — the same "windowed bit" pattern already hypothesized for the G2,
now with a second camera's worth of supporting evidence, and a plausible explanation
for the fps ceiling difference (a full-sensor readout costing more bandwidth than a
windowed crop).

**The UI staleness finding — the reason "nothing worked" looked true at first.** The
operator initially reported none of these dimension_enum sends visibly changed
anything on the camera body. They did: the PRO's on-screen settings display does not
live-update after a `video_format` write. The change genuinely takes effect (the wire
evidence above, confirmed independently by power-cycling the camera afterward — the
new value *is* what's active post-reboot) — the body's menu just keeps showing the old
value until the camera is rebooted. This is now recorded in
`commands.video_format`'s provenance notes as a standing caveat: on this
camera/firmware, a static on-screen display after a video_format write is **not**
reliable evidence the write failed. Trust the wire (a fresh `recording_format` or
`codec_quality` report) or a power cycle, not a glance at the still-displayed menu.
Whether this staleness also affects the G2 has not been tested — the G2's own
video_format verification runbook (§8) never happened to hit this ambiguity, since its
`CameraSession.set_video_format()` round trips were confirmed via the wire the same way
this section's evidence was, not via an on-screen check.

**Still open (as of the sweep above):** every command block and lookup table
transcribed here stays `CANDIDATE`, not `VERIFIED` — no write+echo cycle has been
attempted through `CameraSession` yet on this camera (the equivalent of the G2's
§8/§10 promotion, via `examples/change_codec.py`).

**Update, 2026-07-21 (same day): quality-variant names now confirmed.** An active
send sweep across every candidate id for both codecs
(`tools/control/send_settings_command.py --packet codec_quality`), with the operator
reading the resulting on-screen quality label after each, produced a full mapping:

| Codec | Variant ids → names |
|---|---|
| ProRes | `0`=HQ, `1`=422, `2`=LT, `3`=PXY |
| BRAW | `0`=Q0, `1`=Q5, `2`=3:1, `3`=5:1, `4`=8:1, `5`=12:1, `7`=Q1, `8`=Q3 (`6` not tested/not offered) |

Both codecs' `0` and `3`/`5:1` ids match the G2's own ids for the same names exactly
(`HQ=0` on both; `Q0=0` and `5:1=3` on both) — the same cross-model numbering
consistency every other confirmed value in this section has shown. None of these
sweep sends' own capture windows happened to show a confirming `0x0A/0x00` echo (each
caught the camera's lens-metadata burst on category `0x0C` instead — new protocol
surface, unrelated to settings, not investigated further here) — so this mapping
rests on the operator's direct on-screen read, not yet a wire-verified write+echo
round trip. That gap, and the full `CameraSession` promotion, remain the two open
items for `POCKET_6K_PRO v8.6`'s settings families.

**Update, 2026-07-22: one `fps_modes` entry beyond `50` confirmed, from an eight-way
sweep that mostly missed.** `tools/sniffers/sniffer_settings.py --actions
fps_23_98,fps_24,fps_25,fps_29_97,fps_30,fps_50,fps_59_94,fps_60` was run to fill in
the rest of the fps table (`25` in particular, needed to unblock a
`recording_format --fps 25` redundant-write probe that the profile couldn't build
yet). Only one of the eight windows actually caught a `recording_format` (`0x01/0x09`)
report — `fps_29_97`: `fps_int=30, frame_flags=0x13 (19)`, the identical NTSC-drop
signature the G2's own `"23.98"` entry uses (`fps_int` rounded up, `frame_flags=19`).
Added as `fps_modes."29.97"`; `m_rate=1` is inferred by that same G2 pattern, not yet
independently observed via a `video_format` send on this camera. The other seven
windows — including `fps_25`, the one actually needed — caught unrelated bursts
instead (new, uninvestigated protocol surface: `0x0A/0x01`, `0x01/0x10`, `0x04/0x07`,
`0x03/0x00`, `0x0A/0x05`, `0x09/0x08`) and produced no usable data; `fps_modes."25"`
is still missing and needs a re-run, ideally sweeping fewer actions at once so each
window has a better chance of catching the report before it closes.

**Update, same day: the re-run landed all eight.** The identical sweep, run again,
caught a genuine `recording_format` report in every one of the eight windows this
time (all at the currently-active 4K DCI resolution) — a clean, fully consistent
dataset, including the `25` entry that was actually needed:

| fps label | `fps_int` | `frame_flags` |
|---|---|---|
| `23.98` | 24 | `0x13` (19, NTSC) |
| `24` | 24 | `0x10` (16, exact) |
| `25` | 25 | `0x10` (16, exact) |
| `29.97` | 30 | `0x13` (19, NTSC) |
| `30` | 30 | `0x10` (16, exact) |
| `50` | 50 | `0x10` (16, exact — at 4K DCI; see the `"50"` entry's own note on the resolution-dependence already flagged) |
| `59.94` | 60 | `0x13` (19, NTSC) |
| `60` | 60 | `0x10` (16, exact) |

Every NTSC/exact pair shares the same `fps_int` (24/24, 30/30, 60/60) and is only
distinguished by `frame_flags` — `0x13` for the NTSC-drop member, `0x10` for the exact
member — exactly the G2's own convention, and `29.97`'s values now independently
reconfirmed twice (once from the earlier partial sweep, once from this full one, both
identical). All eight are transcribed into `fps_modes`; `m_rate` stays inferred by the
G2's pattern (`1` for the three NTSC entries, `0` for the rest) rather than observed —
no `video_format` write has been sent at any of these fps values yet. The command that
originally motivated this whole sweep now builds correctly:
`send_settings_command.py --packet recording_format --resolution "4K DCI" --fps 25`.

**Update, same day: that command run for real, with `--repeat 2` — first genuine
`recording_format` write+echo cycle on this camera, and another lens-burst timing
wrinkle.** Real change (2.8K 17:9/50fps → 4K DCI/25fps, BRAW 8:1 unchanged). Send 1's
window caught only the lens-metadata burst again — no `0x01/0x09` — but send 2's
window opened with one arriving within ~150ms of send 2 being issued, far too fast to
be a genuine response to send 2 itself. Read as send 1's real echo, delivered late
(past send 1's 3-second window) by the same burst-congestion pattern that's dominated
several recent captures on this camera: decoded `(fps_int, width, height,
frame_flags)=(25, 4096, 2160, 0x10)`, an exact match for the request and for
`fps_modes."25"` — a stronger, active-send confirmation of that entry on top of the
passive sweep. No second, fresh `0x01/0x09` appeared anywhere else in send 2's window
— consistent with `recording_format` sharing the same no-echo-on-redundant-write
behavior already established for this family (§11, §14) and for `codec_quality` here.
`commands.recording_format`'s provenance now records both the passive and this active
confirmation; still `CANDIDATE`, not `VERIFIED` — that still needs a `CameraSession`
round trip, not a raw `send_settings_command.py` send.

## 16. POCKET_6K_PRO v8.6 — ProRes/4K DCI is unreachable via `recording_format` (2026-07-22)

§15 left the `CameraSession` round trip (`examples/change_codec.py`) as the last open
item before promoting the PRO's settings families to `VERIFIED`. Running it surfaced
two distinct problems, one timing-related and already understood, and one genuinely
new.

### First: the default 3s echo timeout produces a false negative here too

The first `change_codec.py` attempt (default `echo_timeout_s=3.0`) targeted
`ProRes 422 4K DCI @ 25` and immediately failed: `set_video_format`'s own guard raised
`BMDVerificationError` after 3s with no echo on any of `(1,0)`, `(1,9)`, `(10,0)`.
The capture shows why — the camera's lens-metadata burst (category `0x0C`, `docs`
already flagged this in `CLAUDE.md`'s Verification Strategy section as a hypothesis)
dominated the window, and the genuine `0x01/0x09` confirmation (payload decoding to
`fps=25, width=3840, height=2160` — UHD, the proxy resolution `set_camera_format`
substitutes when a codec has no `dimension_enum` for the literal target) didn't arrive
until ~4.2s after the write, past the 3s timeout. This is the same lens-burst delay
pattern already documented for `codec_quality`/`recording_format` sends in §15 and in
`CLAUDE.md`'s known-risk paragraph — now directly confirmed against a real
`CameraSession` write, not just tooling. Re-running with `echo_timeout_s=6.0` avoided
this false negative and exposed the real problem underneath.

### Second: `recording_format` never confirms a retarget to 4K DCI while ProRes is active

With `echo_timeout_s=6.0`, two independent `change_codec.py` runs — one starting from
BRAW 8:1/5.7K 17:9/60fps, one starting from BRAW 5:1/4K DCI/25fps — both targeted
`ProRes 422 4K DCI @ 25` and both failed the same way, at the same step:

1. `set_video_format`'s proxy write to `(UHD, ProRes, 25)` (dimension_enum `6`, the
   only ProRes enum near 4K DCI — see the `resolutions` table) landed and echoed
   promptly: a fresh `0x01/0x09` report decoding to `fps=25, width=3840, height=2160,
   frame_flags=0x10` in both runs, well inside the window.
2. `set_codec_quality`'s write to ProRes/422 landed and echoed promptly too: a fresh
   `0x0A/0x00` report decoding to `codec_id=2, variant_id=1` in both runs.
3. `set_recording_format`'s write to retarget resolution to 4K DCI —
   `TX: FF 0E 00 01 01 09 82 00 19 00 19 00 00 10 70 08 10 00`, decoding to
   `fps=25, width=4096, height=2160, frame_flags=0x10` — produced **zero** fresh
   `0x01/0x09` reports over the full 6.0s window in both runs. The only `0x01/0x09`
   packet seen in either window was one stale, byte-identical duplicate of the prior
   UHD report (`width=3840`), plus the ambient `0x09/0x00` storage telemetry that
   free-runs regardless of any write. Both runs raised `BMDVerificationError` from this
   step.

Steps 1 and 2 prove the camera was genuinely and confirmedly in `ProRes/422/UHD` —
not still mid-transition, not stuck on stale state — when step 3's write went out.
Step 3 is a resolution-only change within the same codec family the camera was already
verified to be in, and it simply never confirms.

The identical target reached via BRAW instead succeeded immediately, every time, in
the same script runs: `dimension_enums.BRAW.4K DCI = 8` lets `set_video_format` land
directly on 4K DCI in one step, so `set_recording_format`'s retarget is never even
invoked for that path. This isolates the failure specifically to `recording_format`
retargeting resolution while ProRes is active — not to 4K DCI in general, not to
`video_format`, and not to a timing artifact (the 6s budget gave more than enough room
for the same lens-burst delay that explained the first false negative).

**A third, superficially similar result is not independent evidence.** A standalone
`send_settings_command.py --packet recording_format --resolution "4K DCI" --fps 25`
probe also produced zero echoes over an 8s window. But it ran immediately after a
`change_codec.py` invocation whose own BRAW combo had just succeeded, landing the
camera at `BRAW 5:1/4K DCI/25fps/frame_flags=0x10` — an exact byte-for-byte match of
the probe's own request. Its silence is fully explained by the already-confirmed
redundant-write no-echo behavior (§11, §14), not by anything ProRes-specific, and
doesn't add to the evidence above. The two `change_codec.py` failures, whose starting
states are independently confirmed to differ from the target and from each other, are
what the conclusion rests on.

### Consequence

This makes the PRO's ProRes/4K DCI gap worse than the G2's: the G2's two-step proxy
workaround (`video_format` to a reachable resolution, then `recording_format` to nudge
the rest of the way — §7-§9) is documented as actually reaching 4K DCI/ProRes on that
camera. On the PRO, the same workaround's second step never confirms, so
`set_camera_format("ProRes", <a 422/HQ/etc variant>, "4K DCI", <any fps>)` currently
always raises `BMDVerificationError` on this camera. No code change has been made to
special-case or paper over this — it's recorded as a real, camera-specific protocol
limitation in `commands.recording_format`'s provenance notes and the `"4K DCI"`
resolution's `_comment` in `payloads/models/POCKET_6K_PRO_v8.6.json`. Whether the
camera has *any* way to reach ProRes/4K DCI (a different dimension_enum candidate not
yet tried, a different write ordering, or a genuine firmware restriction) is still
open; no further candidates have been searched yet, mirroring where the G2's own
4K DCI/ProRes search was left before its proxy workaround was found (§7-§9).

All three settings families remain `CANDIDATE` on this camera — this finding blocks
promoting `recording_format` (and by extension the ProRes/4K DCI combination of
`video_format`) to `VERIFIED` via `change_codec.py`, since that script's ProRes/4K DCI
combo cannot currently pass. A `VERIFIED` promotion for the combinations that do work
(BRAW paths, and any ProRes/resolution pair reachable directly via a `dimension_enum`
without needing the `recording_format` retarget step) is a separate, still-unattempted
step.

**Update, same day: the target state itself is confirmed real — the gap is in our
write path, not the camera.** Three independent passive captures
(`tools/sniffers/sniffer_settings.py --actions prores_uhd_to_4kdci` and
`prores_hd_to_4kdci`) of the operator manually switching the body's own menu from
ProRes/UHD or ProRes/HD into ProRes/4K DCI each caught:

```
FF 0E 00 00 01 09 02 02 18 00 18 00 00 10 70 08 10 00
  category=0x01 param=0x09  ->  fps=24, width=4096, height=2160, frame_flags=0x10

FF 06 00 00 0A 00 01 02 02 00
  category=0x0A param=0x00  ->  codec_id=2 (ProRes), variant_id=0 (HQ)
```

landing in the same window, right next to each other — the camera itself, confirmed
via both watched channels, genuinely holding **ProRes/HQ, 4K DCI, 24fps**. This is the
exact wire shape already expected for that state, and it rules out the alternative
explanation that the camera's firmware simply refuses the combination outright: it
plainly does not. What it rules *in* is that the failure documented above is specific
to how this codebase tries to reach that state over BLE (a still-undiscovered
`dimension_enum`, wrong write framing for this particular transition, or a missing
intermediate step) — not a hard camera-side wall.

This doesn't hand over a working write, though: a body-menu change never touches
`OUTGOING_CONTROL` at all — it's internal to the camera, which simply reports the new
state on `INCOMING_CONTROL` afterward the same way any other change does. These were
pure listen-only captures with no TX to replay. The next concrete step this points to
is the exhaustive `dimension_enum` sweep flagged as still-undone above (§16, "Second"),
now with higher expected payoff since the target is proven reachable — plus a quick,
cheap check first: resending `dimension_enum 0x08` (BRAW's known 4K DCI value) while
*currently in ProRes* rather than BRAW, in case the enum's meaning turns out to be
context-dependent rather than a fixed global table.

**Update, same day: the `0x00`-`0x16` sweep is exhausted — no match, matching the G2's
own exhausted result in this range.** `tools/control/sweep_dimension_enum.py --fps 25
--target-resolution "4K DCI" --target-codec ProRes` swept the 15 untried candidates
left after excluding the 8 already-known enums (`0x01, 0x02, 0x04, 0x05, 0x07, 0x09,
0x0A, 0x0B, 0x0C, 0x0E, 0x10, 0x11, 0x15, 0x16`, plus `0x00`). 14 of 15 produced **zero**
`recording_format`/`codec_quality` reports at all — not even a stale duplicate, only the
ambient `0x09/0x00` storage telemetry every window shows regardless of any write. That's
a clean negative, the same "no camera change" signature the G2's own exhausted
`0x01`-`0x16` search got for its own untried candidates (§7).

The one exception, `0x00`, reported `(fps=24, 1920x1080, flags=0x00)` with
`codec_quality (codec_id=2, variant_id=0)` — ProRes/HQ/HD/24fps. Two things mark this as
a false positive rather than a genuine response: the write requested `fps=25` but the
report shows `fps=24`, and the reported state exactly matches an unrelated passive
capture from ~25 minutes earlier in the same session (the `prores_hd_to_4kdci` capture
above, which recorded the camera already sitting at ProRes/HQ/HD/24fps) — consistent
with leftover connect-burst state rather than a fresh transition caused by this write.
Moot for the ProRes/4K DCI goal either way, since HD/ProRes already has a confirmed
enum (`0x03`) — not worth chasing as a second path to the same resolution.

**Consequence:** the PRO's `0x00`-`0x16` `dimension_enum` space is now exhausted with
the same negative result as the G2's, weakening the "a still-undiscovered enum in this
range" hypothesis on both cameras rather than just one. Candidate next steps, roughly
in order of promise:

1. **Sweep beyond `0x16`** — neither camera's search has covered this
   (`sweep_dimension_enum.py --range 0x17 0x1F ...` or similar; trivial with the tool
   now that it exists).
2. **Retry `recording_format`'s retarget write with `data_type=0x02`** instead of the
   claimed write byte `0x82` (the same spec discrepancy already documented in §3) — a
   more promising lead now that the enum-search branch is weaker evidence than before.
3. **Investigate `video_format`'s two "unexplained trailing zero" elements** as a
   possible second axis alongside `dimension_enum` — never tested with a nonzero value
   on either camera.

**Update, 2026-07-23: `0x17`-`0x1F` swept too — still no match, 32 values now
exhausted.** `tools/control/sweep_dimension_enum.py --range 0x17 0x1F --fps 25
--target-resolution "4K DCI" --target-codec ProRes` covered the next 9 candidates.
All 9 produced **zero** `recording_format`/`codec_quality` reports — not even a
questionable one this time, unlike the `0x00` false positive in the prior range —
only the ambient `0x09/0x00` storage telemetry. `0x00`-`0x1F` (32 values) is now fully
exhausted on this camera with no ProRes/4K DCI match anywhere in it.

This further weakens candidate 1 above (sweeping wider) as the most promising next
step: two full 16-value ranges in a row have produced nothing, on top of the G2's own
exhausted `0x01`-`0x16` search finding nothing either. `dimension_enum` is a single
byte, so the space isn't unbounded, but continuing to guess further into `0x20`+
without new evidence is a weaker bet than it was before this run. **Candidates 2 and 3
above — retrying `recording_format` with `data_type=0x02`, and testing
`video_format`'s unexplained trailing elements with a nonzero value — are now the
leading hypotheses**, since both target a different axis of the packet than the one
just exhausted twice.

**Update, 2026-07-23/24: candidate 2 (`data_type=0x02`) ruled out.**
`tools/control/send_settings_command.py --packet recording_format --resolution
"4K DCI" --fps 25 --data-type INT16` retried the exact same retarget write with wire
data-type byte `0x02` (the camera's own report byte) instead of the claimed write
byte `0x82`. The first attempt used the tool's default 3s window and was inconclusive
— the lens-metadata burst dominated it, the same confound already documented
elsewhere in this section. A follow-up with `--listen-seconds 8` settled it: the
entire 8-second window contained exactly **one** `0x01/0x09` report — decoding to
`(fps=24, width=1920, height=1080, flags=0)`, HD, the camera's pre-write state, not a
match for the requested 4K DCI/25fps — followed by nothing but ambient `0x09/0x00`
storage telemetry for the remaining ~5.5s. Zero fresh confirming reports over a full
window is the exact same signature already established for `0x82`. **`data_type`
(`0x02` vs `0x82`) is not the cause of this failure — ruled out.**

This leaves candidate 3 — `video_format`'s two unexplained trailing zero elements —
as the only untried lead from this list. Testing it needs a small tooling change
first: `encode_video_format` hardcodes those two elements to `0, 0` and
`send_settings_command.py` has no flag to override them (unlike `--dimension-enum`
and now `--data-type`, both already exposed). Beyond that, the passive-capture
evidence (this section's earlier addendum) remains the strongest lead of all: it's
the only approach that has actually observed the camera *in* the target state, rather
than probing blind — a closer look at exactly which channels report immediately
around a body-menu-driven ProRes/4K DCI transition (not just `recording_format` and
`codec_quality`, but everything else in that window) may turn up a detail the three
ruled-out hypotheses above missed entirely.

**Update, 2026-07-24: the trailing-elements tooling now exists.**
`encode_video_format` (`protocol/categories/settings.py`) gained overridable
`extra1`/`extra2` parameters (default `0, 0`, matching every real capture so far),
and `send_settings_command.py` gained `--video-format-extra E1 E2`, mirroring
`--dimension-enum`/`--data-type`'s discovery-grade probe pattern exactly — default
unset, every existing invocation stays byte-for-byte unchanged, and the override is
recorded in the send's label for evidentiary traceability:

```
python tools/control/send_settings_command.py \
    --model-key POCKET_6K_PRO --firmware v8.6 \
    --packet video_format --resolution UHD --codec ProRes --fps 25 \
    --video-format-extra 1 0
```

Not yet tried on real hardware — this is candidate 3 finally made testable, not a
result. As with the other probe flags, watch for a matching `recording_format`/
`codec_quality` report rather than the on-screen display (unreliable on the PRO,
§15), and start with a longer `--listen-seconds` (8+) given the lens-burst timing
confound has produced a false negative on this exact camera before (this section,
above).

**Update, same day: probed on real hardware — no support for the hypothesis so
far.** Four `(extra1, extra2)` pairs were tried, all against `(UHD, ProRes, 25fps,
dimension_enum=6)` with `--listen-seconds 10`:

| `(extra1, extra2)` | Result |
|---|---|
| `(1, 0)` | **Confirmed 2/2** — fresh `0x01/0x09` report, `width=3840` (UHD, exactly as requested), `codec_quality` unchanged (ProRes) both times |
| `(2, 0)` | Zero response over the full 10s window |
| `(0, 1)` | Zero response over the full 10s window |
| `(1, 1)` | Zero response over the full 10s window |

`(1, 0)` proves the override mechanism itself is safe — the camera accepts it and
applies the *requested* enum's resolution exactly, no corruption or redirection —
but it still landed UHD, not 4K DCI. The other three produced the same "silently
rejected" signature already established for out-of-range `dimension_enum`
candidates during the exhaustive sweep (§16 above) — not `recording_format`'s
"accepted but unconfirmed" signature. That distinction matters: it suggests these
three specific values are invalid to the camera outright (perhaps `extra1`/`extra2`
only tolerate `0` or `1`, or only in certain combinations), not that they're
quietly doing something interesting the tool can't see.

**Consequence: this hypothesis has no supporting evidence either**, on top of the
`dimension_enum` sweep and the `data_type` retry both coming back empty. All three
candidates from the original ranked list (§16 above) have now been tried on real
hardware with no match. The strongest remaining lead is no longer "guess another
write parameter" — it's the passive-capture evidence from earlier in this section:
the only approach that has actually *observed* the camera in the target state.
A closer look at every channel active in that capture window (not just
`recording_format`/`codec_quality`, the only two decoded so far) — including
whatever the lens-metadata burst and any other category carries — may hold a detail
none of the three ruled-out write-parameter hypotheses could have found, since none
of them could ever see past "did a matching report arrive or not."

**Update, 2026-07-24: the full-channel decode found nothing new either.** Every
notification across all three passive-capture windows (the `prores_uhd_to_4kdci`
and `prores_hd_to_4kdci` captures referenced in `commands.recording_format`'s
provenance) was decoded with `protocol/codec.py`'s own `decode_packet` — not just
the two channels already extracted. Every `(category, parameter)` pair present
falls into one of three buckets:

- **The transition marker** — `0x01/0x09` (`recording_format`) is the *only*
  channel whose value changes in step with the transition:
  `(fps=24, w=1920, h=1080, flags=0)` before → `(fps=24, w=4096, h=2160, flags=0x10)`
  after. `0x0A/0x00` (`codec_quality`) stays `[2, 0]` (ProRes/HQ) on both sides —
  confirms the codec didn't change, but doesn't move with the transition either.
- **Free-running telemetry** — `0x09/0x00` and `0x09/0x02` drift by small amounts
  every ~1s in every window regardless of any activity (the storage write-margin
  signal and a sibling counter, both already known ambient noise).
- **One-time static dump** — everything else (`0x00/0x01`, `0x00/0x02`, `0x00/0x03`,
  `0x00/0x07`, `0x01/0x02`, `0x01/0x07`, `0x01/0x08`, `0x01/0x0A`, `0x01/0x0B`,
  `0x01/0x0E`, `0x01/0x0F`, `0x01/0x10`, `0x03/0x00`, `0x03/0x03`, `0x04/0x07`,
  `0x09/0x01`, `0x09/0x05`–`0x08`, `0x0A/0x01`, `0x0A/0x05`, and the full
  `0x0C/0x00`–`0x0F` lens-metadata burst) appears once near connect and never
  changes within any window — camera capability, exposure, and lens info, not
  resolution-related.

No channel besides `recording_format` itself correlates with the transition. There
is no hidden field, no extra category, nothing on the visible `INCOMING_CONTROL`
surface that reveals *how* the camera internally applies the change — from the
notification side, it's a complete black box. This was the strongest remaining
lead from the original plan, and it's now exhausted too, the same way the three
write-parameter hypotheses were.

**One genuinely new, untried axis this leaves.** Every write attempted so far —
across all three ruled-out hypotheses — used `Operation.ASSIGN` (`0x00`,
`protocol/codec.py`'s `encode_assign`/`encode_assign_elements`). The packet
header format documents a second write-capable operation, `OFFSET` (`0x01`,
see `CLAUDE.md`'s packet structure section), never tried for any settings family
on either camera. Untested, and it's unknown what OFFSET semantics would even mean
for a resolution field — but it varies a genuinely different axis than value,
data-type byte, or trailing elements, all of which are now exhausted. See below
for the tooling this needs.

**Update, 2026-07-24: the OFFSET tooling now exists — plus an important
correction on how to use it.** `encode_assign`/`encode_assign_elements`
(`protocol/codec.py`) and all three settings-family encoders
(`encode_codec_quality`/`encode_video_format`/`encode_recording_format`) gained
an overridable `operation` parameter (default `Operation.ASSIGN`, every existing
caller unaffected), and `send_settings_command.py` gained `--operation NAME`,
mirroring `--dimension-enum`/`--data-type`/`--video-format-extra`'s
discovery-grade probe pattern exactly:

```
python tools/control/send_settings_command.py \
    --model-key POCKET_6K_PRO --firmware v8.6 \
    --packet recording_format --resolution "4K DCI" --fps 25 \
    --operation OFFSET
```

**Read `docs/protocol.md` §4 before running this.** The official spec's stated
meaning for `OFFSET` is "add the payload to the current value," not "assign it" —
so the command above, which reuses `recording_format`'s normal *absolute* payload
(`width=4096`), would ask the camera to add `4096` to whatever width is already
active (e.g. `3840 + 4096 = 7936` from UHD), not set it to `4096`. That's a
different, less meaningful test than the delta this hypothesis actually needs: a
width delta of `4096 - 3840 = 256` (with height/fps deltas of `0`, since only
width changes between UHD and 4K DCI). This tool always builds its payload from
the profile's absolute `resolutions`/`fps_modes` tables — there's no delta-payload
mode — so a faithful OFFSET test currently means picking (or, if needed, adding
in a follow-up) a target whose absolute values already equal the intended delta,
not just resending the 4K DCI target with `--operation OFFSET` and expecting
the spec's stated arithmetic to land there. The first, simpler test (`--operation
OFFSET` with the existing absolute payload) is still worth running — it answers
whether the camera accepts `OFFSET` for this family at all, and some CANDIDATE
values in this protocol have already diverged from the official spec's stated
behavior before (`docs/protocol.md` §3) — but don't read a failure from that test
alone as ruling OFFSET out entirely; the delta-based version hasn't been tried.
Not yet tried on real hardware either way.

**Update, 2026-07-24: the absolute-payload OFFSET test came back with zero
response.** Ran exactly the command above (`--packet recording_format
--resolution "4K DCI" --fps 25 --operation OFFSET`, `--listen-seconds 10`) against
the PRO. TX confirmed the header carried the override correctly
(`FF 0E 00 01 01 09 82 01 19 00 19 00 00 10 70 08 10 00` — operation byte `01`
at offset 7, payload otherwise identical to the ASSIGN version). Over the full 10s
window, only the ambient `0x09/0x00` storage telemetry and the usual lens-metadata/
connect-burst packets appeared — **zero** `0x01/0x09` reports, the same
"zero response" signature already seen for invalid `dimension_enum` candidates and
`video_format` extras, not `recording_format`'s "accepted but unconfirmed"
signature. As predicted in the correction above, this is not conclusive proof
`OFFSET` is unsupported for this parameter — the payload sent was `width=4096`
absolute, which per the spec's stated arithmetic means "request width
`current + 4096`," almost certainly an out-of-range value the camera has every
reason to silently reject regardless of whether `OFFSET` itself works. The delta
test is the one that actually exercises the hypothesis.

**Update, 2026-07-24: `--raw-payload` now exists for the delta test.**
`send_settings_command.py` gained `--raw-payload VALUE [VALUE ...]` (accepts
`0x..` hex or decimal per element), which bypasses `--resolution`/`--codec`/
`--fps`/`--sensor-fps` and the profile's lookup tables entirely and encodes a
literal element sequence as the payload — calling `encode_assign_elements`
(`protocol/codec.py`) directly rather than going through
`encode_recording_format`/`encode_video_format`/`encode_codec_quality`, so no
protocol-layer changes were needed for this flag. It still reads
category/parameter/reserved from the profile's command block, and still composes
with `--data-type`/`--operation`. This makes the actual delta test possible for
the first time:

```
python tools/control/send_settings_command.py \
    --model-key POCKET_6K_PRO --firmware v8.6 \
    --packet recording_format --raw-payload 0 0 256 0 0 \
    --operation OFFSET --listen-seconds 10
```

`[0, 0, 256, 0, 0]` matches `recording_format`'s `[fps_int, sensor_fps_int, width,
height, frame_flags]` shape — `0` for every field with no requested change, `256`
for the width delta (`4096 - 3840`, UHD → 4K DCI). This is the first test that
sends `OFFSET` a payload actually shaped like a delta rather than an absolute
target, and the last untried variant of every hypothesis raised in this section.
Not yet tried on real hardware.

**Update, 2026-07-24: the delta test came back with the same zero response — every
hypothesis in this section is now exhausted.** Ran exactly the command above
against the PRO with `--listen-seconds 10`. TX independently decoded and confirmed
correct before trusting the result: header byte 6 = `0x82` (profile default, no
`--data-type` given), byte 7 = `0x01` (`OFFSET`), payload decodes to
`(0, 0, 256, 0, 0)` — exactly the intended delta, not a mis-encoding. Over the full
10s window: **zero** `0x01/0x09` reports. The only notifications present were the
usual connect-burst tail (`0x0C/0x00`-`0x0F` lens metadata, `0x0A` device-info
strings), the ambient `0x09/0x00` storage telemetry, and one previously-unseen
one-off packet — `0x09/0x08` (`INT8`, payload `[0, 1]`) — almost certainly more
connect-burst noise sharing category `0x09` with the known storage signal, not a
response to this write (it appeared as the very first packet in the window,
before the send had any plausible time to produce a reaction).

This result is meaningfully stronger than the absolute-payload test's. That
earlier silence was explained away as a payload-shape problem: an absolute
`width=4096` sent as an `OFFSET` requests `current + 4096`, almost certainly out
of range, giving the camera an obvious reason to reject it regardless of whether
`OFFSET` itself is supported. This delta test doesn't have that escape hatch — a
`+256` width delta from UHD's `3840` lands exactly in-range at `4096`, the valid
4K DCI width — and it still produced the same "zero response" signature already
seen for invalid `dimension_enum` candidates and invalid `video_format` extras
(not `recording_format`'s established "accepted but unconfirmed" signature for a
genuinely-attempted retarget). The most direct reading: **`Operation.OFFSET`
itself is not acted on for this write**, independent of whether the payload is
absolute or a well-formed delta.

One caveat worth stating plainly: this reading assumes the camera was actually at
UHD (width `3840`) immediately before the send. If it wasn't, a `+256` delta
wouldn't land on `4096` either, and the "in-range" framing above wouldn't hold.
The operator's noted starting state was UHD, consistent with the intended test,
but this wasn't independently re-confirmed by a fresh report inside this window
(the last confirming `recording_format` report predates it) — a residual,
un-eliminated possibility rather than a live concern.

**Every hypothesis raised in this investigation is now exhausted**: the
`dimension_enum` sweep (`0x00`-`0x1F`, two full ranges), the `data_type` byte
retry (`0x02` vs `0x82`), `video_format`'s trailing elements, a full-channel
passive decode of the transition, `OFFSET` with an absolute payload, and `OFFSET`
with a genuine delta payload. None produced a confirming `0x01/0x09` report for
ProRes/4K DCI on `POCKET_6K_PRO v8.6`, despite the passive-capture evidence
(§16 addendum) proving the camera genuinely holds and reports that exact state
when reached through its own body menu — the gap remains isolated to this
codebase's write path, not a camera-side refusal of the combination, but nothing
tried so far has closed it.

**A next diagnostic step, not yet built or run:** every `OFFSET` test so far has
targeted `recording_format` specifically. It's still an open question whether
`Operation.OFFSET` is silently rejected camera-wide (a firmware-level
non-implementation) or only for this particular category/parameter. Sending an
`OFFSET` delta against a family with well-characterized `ASSIGN` echo behavior —
e.g. `codec_quality`, whose "fires only on a genuine applied change, stays silent
on a no-op" behavior is already confirmed on both cameras (§11) — via
`--packet codec_quality --raw-payload 0 1 --operation OFFSET` (a `+1` variant
delta, avoiding the ambiguous zero-delta no-op case) would isolate the two
explanations: a confirming echo there would mean `OFFSET` works in general and
`recording_format` specifically refuses it; continued silence there too would
point at `OFFSET` being unimplemented camera-wide. No code changes needed — the
existing `--raw-payload`/`--operation` flags already support this.
