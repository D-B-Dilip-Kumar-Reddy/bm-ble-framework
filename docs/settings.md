# Settings — codec, quality, resolution, FPS

**Status:** CANDIDATE — three packet families implemented end to end
(`protocol/categories/settings.py`, profile blocks, `CameraSession` methods,
capture tooling). The **report side** of `codec_quality` and
`recording_format` is sniffer-confirmed by this repo's own passive capture
(§5, 2026-07-20). The **write side** of all three families is
operator-confirmed on real hardware (§6, 2026-07-20 active sends) —
including the doc's two central claims (`codec_quality` alone can't switch
codec families; `video_format` can). All 8 known `dimension_enum` values
are now confirmed by a clean, decoded echo too (§7, 2026-07-20 probe
sweep, run after a tooling fix) — only 4K DCI/ProRes's enum remains
unknown. Nothing is hardware-`VERIFIED` yet: every family still needs a
round trip through `CameraSession` itself (not just the raw send tool)
before promotion — see §6/§7 for exactly what's missing.

## Provenance and evidence status

The byte layouts and value tables below were transcribed from
`CODEC_RES_FPS_6K_G2.docx` (operator-supplied, 2026-07-20) — the write-up of
a reverse-engineering effort against a real `POCKET_6K_G2 v7.9`. That makes
them better than [spec] guesses but weaker than this repo's
[sniffer-verified] bar (CLAUDE.md design principle 6: values must originate
from a capture on that camera — this repo has not seen these packets on a
wire it captured itself). They are therefore modeled with
`provenance.status: "CANDIDATE"` in `payloads/models/POCKET_6K_G2_v7.9.json`,
and the operator's own summary was explicit that **not all packets are
reverse-engineered** — known gaps are listed per section below.

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

## 2. Value tables (`POCKET_6K_G2 v7.9` — CANDIDATE)

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

### 4.3 End to end: the session path

```bash
python examples/change_codec.py
```

Runs `set_video_format` + `set_codec_quality` round trips (ProRes and back
to BRAW) through `CameraSession`'s echo verification. §5 established that
body-initiated changes report on `0x01/0x09` and `0x0A/0x00` — both
channels the session methods already await — so verification is *expected*
to work; whether a written command triggers the same reports is exactly
what this step tests. A `BMDVerificationError: no echo received` with the
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

## 8. Code surface

| Piece | Where |
|---|---|
| Packet encoders/decoders | `protocol/categories/settings.py` (`encode_codec_quality`, `encode_video_format`, `encode_recording_format`, matching `decode_*`, `VideoFormat`/`RecordingFormat` dataclasses) |
| Multi-element ASSIGN encoder | `protocol/codec.py` `encode_assign_elements` |
| CANDIDATE data type | `protocol/types.py` `DataType.INT16_ARRAY` (0x82) |
| Profile blocks + tables | `payloads/models/POCKET_6K_G2_v7.9.json` `commands.codec_quality` / `commands.video_format` / `commands.recording_format`, `codecs`, `resolutions`, `fps_modes`; schema `$defs` `codecSpec`/`resolutionSpec`/`fpsModeSpec` |
| Profile accessors | `camera_profile.py` `require_codec` / `require_resolution` / `require_fps_mode` (`CodecSpec`/`ResolutionSpec`/`FpsModeSpec`) |
| Session methods | `session.py` `set_codec_quality` / `set_video_format` / `set_recording_format` (see `docs/session_and_verification.md` for the echo strategy) |
| Tools | `tools/sniffers/sniffer_settings.py` (passive), `tools/control/send_settings_command.py` (active, typed-yes gated) |
| Example | `examples/change_codec.py` |
| Tests | `tests/unit/protocol/categories/test_settings.py`, plus settings cases in `test_codec.py`, `test_types.py`, `test_camera_profile.py`, `test_session.py` |

### Session verification strategy for settings writes

`set_codec_quality` and `set_recording_format` follow the standard
arm-write-await pattern on the command's own (category, parameter).
`set_video_format` arms **two** channels — its own `0x01/0x00` and the
recording_format coordinates `0x01/0x09` — and accepts the first fresh
report on either, because the echo channel for a FORMAT write is
unconfirmed (a mode change plausibly reports as the mode struct on `1/9`).
Payload comparison per channel:

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
