# Blackmagic Camera Control Protocol — full reference

**Status:** reference document — evidence-tagged; only the recording command family is sniffer-verified so far.

The one-stop reference for the protocol this framework speaks: the
**Blackmagic SDI Camera Control Protocol** (the command vocabulary) and the
**BLE camera control layer** (how those commands travel over Bluetooth LE).

## How to read this document — evidence tags

Every claim here carries one of three tags. This matters because CLAUDE.md
design principle 6 forbids using a protocol value in a profile unless it was
sniffed on that exact camera/firmware — the [spec] tables below are a *map
for reverse engineering*, never a source to copy values from.

| Tag | Meaning |
|---|---|
| **[spec]** | From the public *Blackmagic Camera Control Developer Information* document (documents.blackmagicdesign.com/DeveloperManuals/BlackmagicCameraControl.pdf; cross-checked against the machine-readable transcription at github.com/coral/blackmagic-camera-protocol). Not verified over BLE on this repo's hardware. |
| **[sniffer-verified]** | Confirmed by a real BLE capture on `POCKET_6K_G2 v7.9` (see `docs/recording.md`, `docs/packet_structure_and_constants.md`). |
| **[hypothesis]** | A plausible spec↔capture mapping that has NOT been confirmed. Never promote a hypothesis into a profile JSON without a targeted capture. |
| **[external-RE]** | From an operator-supplied external reverse-engineering document against real hardware this repo did not capture itself (currently: the `POCKET_6K_G2 v7.9` settings families, `docs/settings.md`). Stronger than [spec], weaker than [sniffer-verified]; carried in profiles as `provenance.status: "CANDIDATE"` until re-verified by this repo's tooling. |

---

## 1. The BLE camera control layer

### 1.1 GATT services and characteristics

Constants live in `src/bmd_ble/constants.py`. All BMD UUIDs below are
[spec] *and* confirmed present on real hardware by
`tools/query/ble_services_chars.py`.

**Blackmagic Camera Service** — `291d567a-6d75-11e6-8b77-86f30ca893d3`

| Characteristic | UUID | Properties | Purpose |
|---|---|---|---|
| Outgoing Camera Control (`CHARACTERISTIC_OUTGOING`) | `5dd3465f-1aee-4299-8493-d2eca2f8e1bb` | Write | Send camera control packets |
| Incoming Camera Control (`CHARACTERISTIC_INCOMING`) | `b864e140-76a0-416a-bf30-5876504537d9` | **Indicate** | Camera-originated packets: command echoes, state reports, ambient telemetry |
| Timecode (`CHARACTERISTIC_TIMECODE`) | `6d8f2110-86f1-41bf-9afb-451d87e976c8` | Notify | 32-bit BCD timecode, e.g. `09:12:53:10` = `0x09125310`; ticks ~1/s |
| Camera Status (`CHARACTERISTIC_CAM_STATUS`) | `7fe8691d-95dc-4fc5-8abd-ca74339b51b9` | Read/Notify/Write | 8-bit flag field, see §1.2 |
| Protocol Version (`CHARACTERISTIC_PROTO_VER`) | `8f1fd018-b508-456f-8f82-3d392bee2706` | Read | Camera's supported CCU protocol version |
| Device Name (`CHARACTERISTIC_BMD_DEVICE_NAME`) | `ffac0c52-c9fb-41a0-b063-cc76282eb89c` | Write | Name shown in the camera's Bluetooth Setup menu (max 32 chars) |

Standard services: **Generic Access** (`1800`: device name `2a00`,
appearance `2a01`) and **Device Information** (`180a`: manufacturer `2a29` —
always "Blackmagic Design" [spec] — and model `2a24`). Whether these are
readable varies per camera *and per firmware* — recorded per-profile as
`gap_meta_data.readable` / `device_info_meta_data.readable`
([sniffer-verified]: G2 v7.9 exposes neither; PRO v8.6 exposes both; G2 v8.6
exposes both, re-checked on the same physical unit after the v7.9 → v8.6
upgrade — so this is a firmware property, not a model property).

### 1.2 Camera Status byte

[spec] flag values, mirrored in `constants.py`:

| Bit | Meaning |
|---|---|
| `0x01` | Camera Power On |
| `0x02` | Connected |
| `0x04` | Paired |
| `0x08` | Versions Verified |
| `0x10` | Initial Payload Received |
| `0x20` | Camera Ready |

Writing `0x00`/`0x01` powers the camera off/on [spec]. **None of the known
bits encode recording state** — which is why recording verification is
echo-only today (see `docs/session_and_verification.md`).
[sniffer-verified]: on G2 v7.9 the value `0x03` was observed during an
active send-and-capture run; `CAMERA_STATUS` notifications are otherwise
unreliable on that camera (CLAUDE.md).

### 1.3 Bonding and subscription behaviour

- The camera must be **paired/bonded** (PIN shown on the camera's Bluetooth
  menu at first connect) before control characteristics respond usefully
  [spec].
- `INCOMING_CONTROL` uses **Indicate**, not Notify — recorded per-profile as
  `ble.incoming_property` ([sniffer-verified] on both current profiles).
  Bleak's `start_notify` handles either transparently.
- On connect the camera pushes an initial burst of state packets on
  `INCOMING_CONTROL` (the "initial payload"), then keeps reporting: some
  categories arrive continuously (~1/s ambient telemetry — categories
  `0x09` and `0x0C` on G2 v7.9 [sniffer-verified]); discrete state changes
  (e.g. recording) arrive once per change.
- A profile can override the incoming characteristic UUID via
  `ble.characteristic_incoming` (see `docs/payload_profiles.md`); no camera
  has needed it yet.

---

## 2. Packet framing over BLE

Implemented in `protocol/codec.py`; full history in
`docs/packet_structure_and_constants.md`.

```
Byte 0      Fixed prefix 0xFF                       [sniffer-verified]
Byte 1      Length — counts bytes 4.. only          [sniffer-verified]
Byte 2      Command ID (0x00 = change config)       [spec]
Byte 3      Reserved                                (real commands deviate — see below)
Byte 4      Category
Byte 5      Parameter
Byte 6      Data type                               (see §3)
Byte 7      Operation                               (see §4)
Bytes 8+    Payload (little-endian)                 [spec+sniffer]
```

### Where BLE reality diverges from the generic SDI spec

The SDI spec describes packets embedded in SDI blanking; over BLE, real
captures on `POCKET_6K_G2 v7.9` contradict it in four ways
([sniffer-verified] unless noted):

1. **Byte 0 is always `0xFF`, both directions.** Over SDI it is a
   destination device address (0–254, 255 = broadcast) [spec]; over BLE the
   link is point-to-point and the byte is effectively a fixed prefix. A
   systematic decode failure was traced to assuming `0x00` here.
2. **The length byte counts only bytes 4 onwards** (category, parameter,
   data type, operation, payload): `total = declared + 4`. The generic spec
   reads as "everything after byte 1".
3. **No 32-bit padding.** SDI packets are padded to a four-byte boundary
   [spec]; BLE packets arrive exactly `4 + length` bytes (the 9-byte record
   command and 14-byte echo are not multiples of 4).
4. **Reserved (byte 3) is not always `0x00`.** The G2's real recording
   command carries `0x01` there. Profiles record the captured value
   per command block (`reserved` in the schema).

---

## 3. Data types

### 3.1 The coding (`protocol/types.py` — official spec coding)

`protocol/types.py` uses the official document's coding [spec]. One table,
shared by the code, the profile JSONs (`data_type` symbolic names), and this
doc:

| Code | Type | Width (bytes) | `DataType` member |
|---|---|---|---|
| 0 | void / boolean | 0 (void trigger) or 1 per element (boolean; 0 = false) | `VOID` (alias `BOOL`) |
| 1 | int8 (signed byte) | 1 | `INT8` |
| 2 | int16 | 2 | `INT16` |
| 3 | int32 | 4 | `INT32` |
| 4 | int64 | 8 | `INT64` |
| 5 | UTF-8 string | variable | `STRING` |
| 128 | fixed16 (signed 5.11 fixed point) | 2 | `FIXED16` |
| 130 (`0x82`) | int16 array (per-element width 2) | 2 × N | `INT16_ARRAY` — **not official coding**, see below |

**Provenance:** data-type bytes sniffer-verified over BLE so far:

- `0x01` (`INT8`) — the recording command/echo, agreeing with the spec's
  transport-mode parameter (§5, 10.1) being int8; also the codec report
  (10.0, 2026-07-20 settings capture). `POCKET_6K_G2 v7.9`.
- `0x02` (`INT16`) — camera reports on 1.9 (recording format, five int16
  elements decoding exactly per the spec layout) and on the category-9
  ambient parameters (2026-07-20 settings capture). `POCKET_6K_G2 v7.9`.
- `0x03` (`INT32`) — a 1.11 shutter-angle report (`18000` = 180.00°,
  matching the spec's degrees × 100 exactly; 2026-07-20 settings capture).
  `POCKET_6K_G2 v7.9`.
- `0x00` (`VOID`/`BOOL`) — both flavors of code 0, in the 2026-07-27
  photo-capture connect bursts on **both** cameras: payloadless void
  reports on 0.1's coordinates (`FF 04 00 00 00 01 00 02` — one-shot AF, a
  void trigger in the spec), and a one-byte boolean-shaped report on
  `0x0C/0x04`.
- `0x05` (`STRING`) — the `0x0C` lens-metadata strings ("Canon EF-S
  18-55mm f/3.5-5.6 IS STM", "f4.0", "26mm", …) decoding as clean UTF-8;
  2026-07-27 photo captures, both cameras (the PRO's lens bursts during
  the 2026-07-22 settings work were the first sighting, `docs/settings.md`
  §15).
- `0x80` (`FIXED16`) — a `0x00/0x02` aperture report whose leading int16
  ÷ 2048 lands exactly on the lens's true aperture: G2 `0x2000`/2048 =
  AV 4.0 → f-number √(2^4.0) = f/4.0, matching the "f4.0" lens *string* in
  the same burst; PRO `0x1CEA`/2048 = AV 3.61 → ≈f/3.5, a plausible real
  aperture but with no in-window string to cross-check against, unlike the
  G2's (2026-07-27 photo captures). Both reports carried **four** bytes
  where the spec table lists one fixed16 element — the trailing int16 was
  `0` on both cameras, unexplained.

Only `0x04` (`INT64`) among the official codes has never been observed on
hardware. The fixed16 element-count surprise above is the standing reminder
to keep checking byte 6 and the payload length against this table before
trusting a decode.

**`0x82` (`INT16_ARRAY`)** is not in the official document at all. It was
reported on `POCKET_6K_G2 v7.9`'s recording-format write packet (category
`0x01` parameter `0x09`, five int16 elements) by an external
reverse-engineering effort — CANDIDATE evidence, not yet re-verified by
this repo's capture tooling. [hypothesis] `0x82 = 0x80 | 0x02` ("array
flag + int16") — unconfirmed, and in tension with `FIXED16` already
occupying `0x80`. See `docs/settings.md` §3.

**History:** this repo originally used an *assumed* enum
(`0=void, 1=bool, 2=int8, …, 7=fixed16`) that disagreed with the official
coding from code 2 upward, and this section used to document that as an open
discrepancy. The enum was remapped to the official coding on 2026-07-09. The
change was wire-compatible — the recording command's captured byte `0x01`
stayed `0x01`, only its symbolic name changed from `BOOL` to `INT8` (profile
JSONs record the relabelling in `provenance.notes`).

### 3.2 fixed16

[spec] Signed 5.11 fixed point: `encoded = round(real × 2048)`, giving
range −16.0…+15.9995 in a little-endian int16. Used by most continuous
controls (focus, aperture, audio levels, color correction). This repo
currently decodes fixed16 as a raw int16 — convert with `/ 2048` once a
fixed16 parameter is actually consumed.

---

## 4. Operation types (header byte 7)

| Value | Name | Meaning | Evidence |
|---|---|---|---|
| `0x00` | ASSIGN | Set the parameter to the payload value | [spec]; used by every controller command captured so far [sniffer-verified] |
| `0x01` | OFFSET | Add the payload to the current value (toggle for booleans) | [spec]; never yet observed over BLE — probed for the first time via `tools/control/send_settings_command.py --operation OFFSET` (2026-07-24, `docs/settings.md` §16), not yet tried on real hardware. Per this spec meaning, testing it faithfully means sending the *delta* from the current value, not the same absolute target payload `ASSIGN` uses — e.g. retargeting `recording_format` from UHD to 4K DCI would need a width delta of `4096-3840=256`, not `4096` |
| `0x02` | CAMERA_REPORT | Camera reporting a value on `INCOMING_CONTROL` | Not in the public spec. [sniffer-verified]: every camera-originated notification captured so far uses it. Stored per-command as `echo_operation` in profiles |

---

## 5. SDI protocol categories and parameters

All tables in this section are **[spec]** — transcribed from the official
document (August 2025 edition) via the machine-readable
coral/blackmagic-camera-protocol transcription. They are the *starting
point* for a sniffer session, not values to copy into profiles: parameter
availability, payload encodings, and even category numbers can differ per
camera/firmware (the G2's recording payload `2` on a "boolean" parameter is
the cautionary example). Multi-element parameters list their elements in
payload order; all values little-endian.

### Category overview

| Category | Name | This repo |
|---|---|---|
| 0 | Lens | — |
| 1 | Video | `protocol/categories/settings.py`: video_format (1.0) and recording_format (1.9), CANDIDATE — see `docs/settings.md` |
| 2 | Audio | — |
| 3 | Output | — |
| 4 | Display | — |
| 5 | Tally | — |
| 6 | Reference | — |
| 7 | Configuration | — |
| 8 | Color Correction | — |
| 9 | (undocumented) | mostly ambient ~1/s telemetry, meaning unknown [sniffer-verified] — except parameter 1, a CANDIDATE write-margin warning, see §9 below |
| 10 | Media | `protocol/categories/recording.py` (10.1); `protocol/categories/settings.py` codec_quality (10.0, CANDIDATE — see `docs/settings.md`); `commands.photo` (10.3) VERIFIED on both `POCKET_6K_G2 v7.9` and `POCKET_6K_PRO v8.6` but with no `protocol/categories/media.py` yet — see below and `docs/photo_capture.md`; future: playback (10.2) |
| 11 | PTZ Control | — |
| 12 | Metadata | future: metadata reads; also ambient telemetry observed on G2 v7.9 (`0x0C`) [sniffer-verified] |

### Category 0 — Lens

| Param | Name | Type | Range | Meaning |
|---|---|---|---|---|
| 0.0 | Focus | fixed16 | 0.0–1.0 | 0.0 = near, 1.0 = far |
| 0.1 | Instantaneous autofocus | void | — | trigger one-shot AF |
| 0.2 | Aperture (f-stop) | fixed16 | −1.0–16.0 | Aperture Value; fnumber = √(2^AV) |
| 0.3 | Aperture (normalised) | fixed16 | 0.0–1.0 | 0.0 = smallest, 1.0 = largest |
| 0.4 | Aperture (ordinal) | int16 | 0–n | steps through available stops |
| 0.5 | Instantaneous auto aperture | void | — | trigger one-shot auto iris |
| 0.6 | Optical image stabilisation | boolean | — | true = enabled |
| 0.7 | Set absolute zoom (mm) | int16 | 0–max | focal length in mm |
| 0.8 | Set absolute zoom (normalised) | fixed16 | 0.0–1.0 | 0.0 = wide, 1.0 = tele |
| 0.9 | Set continuous zoom (speed) | fixed16 | −1.0–+1.0 | −1 = wide fast, 0 = stop, +1 = tele fast |

### Category 1 — Video

| Param | Name | Type | Elements / meaning |
|---|---|---|---|
| 1.0 | Video mode | int8 ×5 | [0] frame rate (24/25/30/50/60), [1] M-rate (0/1), [2] dimensions, [3] interlaced (0/1), [4] colorspace (0 = YUV) — CANDIDATE external-RE evidence on G2 v7.9 (`docs/settings.md` §1.2): this is the "video_format" packet whose dimensions enum locks resolution AND codec family, i.e. the BRAW↔ProRes switch |
| 1.1 | Gain (legacy, ≤ Camera 4.9) | int8 | 1–128: 1×/2×/4×/…/128× |
| 1.2 | Manual White Balance | int16 ×2 | [0] color temp (K), [1] tint (−50–50) |
| 1.3 | Set auto WB | void | calculate + set auto white balance |
| 1.4 | Restore auto WB | void | reapply last auto WB |
| 1.5 | Exposure (µs) | int32 | 1–42000 µs |
| 1.6 | Exposure (ordinal) | int16 | steps through available exposures |
| 1.7 | Dynamic Range Mode | int8 | 0 = film, 1 = video, 2 = extended video |
| 1.8 | Sharpening level | int8 | 0 = off, 1 = low, 2 = medium, 3 = high |
| 1.9 | Recording format | int16 ×5 | [0] file frame rate, [1] sensor frame rate, [2] frame width, [3] frame height, [4] flags (file-M-rate, sensor-M-rate, sensor off-speed, interlaced, windowed) — camera *reports* on this parameter are [sniffer-verified] on G2 v7.9 (2026-07-20): exact element order, data-type byte `0x02`, flags values `0x0000`/`0x0010`/`0x0013` decoding precisely as the spec bitfield (bit 4 = windowed, resolution-dependent). The *write* packet remains CANDIDATE external-RE evidence, claimed with data-type byte `0x82` (not official coding). See `docs/settings.md` §1.3, §5. New corroboration 2026-07-27 (`docs/photo_capture.md` §10.1): the windowed bit tracked exactly which ProRes "Sensor Area" was selected (full-sensor 6K → clear, cropped 2.8K/5.7K → set) while width/height stayed pinned to the unrelated active video resolution (HD) — consistent with, but not a full 3-way encoding of, the sensor-area choice; width/height and codec/variant do not encode sensor area at all on this or the `codec_quality` channel. Independently reconfirmed on `POCKET_6K_PRO v8.6` the same day (`docs/photo_capture.md` §10.3): identical clear-only-for-full-sensor-6K pattern, on a different baseline flags byte (`0x10`/`0x00` vs the G2's `0x13`/`0x03`) — the bit-4 boundary, not the exact byte value, is what's common across cameras. **Reproducibility CONFIRMED** 2026-07-27 (`docs/photo_capture.md` §10.4, PRO): an interleaved A-B-A-B sensor-area sweep toggled this bit byte-identically (`0x0010`↔`0x0000`) on demand, twice each way — a clean, repeatable, on-demand toggle, not a one-off correlation. **Search space closed, 2026-07-27** (`docs/photo_capture.md` §10.6): the operator searched the full 115-page official spec document directly (every "sensor" occurrence, 26/26) and found no parameter named or resembling "Sensor Area" anywhere — every "sensor"-prefixed term in the entire document belongs to this same 1.9 struct (`[1] sensor frame rate`, flags bits `sensor-M-rate`/`sensor-off-speed`) or 1.12's "current sensor frame rate," none of them about a spatial crop/readout-region selection. This bit's own name, "windowed mode," is the closest and only officially-documented concept related to sensor readout area in the entire spec — this codebase's independently-derived "windowed bit" hypothesis (from wire behavior alone, before this search) turns out to be exactly this official field. **Confirmed READ-ONLY, 2026-07-27** (`docs/photo_capture.md` §10.7, `POCKET_6K_PRO v8.6`): an isolated ASSIGN write flipping only this bit (fps/width/height held at an already-confirmed state) produced no echo and — confirmed via a photo taken immediately before and after, both measuring identical dimensions on the SD card — no physical effect either. Genuine ground-truth confirmation of no effect, not just wire silence. This closes the Sensor Area investigation: the bit is a real, reproducible read signal but not independently writable by any means tried |
| 1.10 | Auto exposure mode | int8 | 0 = manual, 1 = iris, 2 = shutter, 3 = iris+shutter, 4 = shutter+iris |
| 1.11 | Shutter angle | int32 | 100–36000 (degrees × 100) — a camera report is [sniffer-verified] on G2 v7.9: int32 `18000` = 180.00° emitted right after an fps change (2026-07-20 settings capture) |
| 1.12 | Shutter speed | int32 | 1–5000 (fraction of 1s: 50 → 1/50) |
| 1.13 | Gain (dB) | int8 | −128–127 dB |
| 1.14 | ISO | int32 | ISO value |
| 1.15 | Display LUT | int8 ×2 | [0] selected LUT, [1] enabled (0/1) |
| 1.16 | ND Filter | fixed16 | 0–16, f-stops |

### Category 2 — Audio

| Param | Name | Type | Meaning |
|---|---|---|---|
| 2.0 | Mic level | fixed16 | 0.0–1.0 |
| 2.1 | Headphone level | fixed16 | 0.0–1.0 |
| 2.2 | Headphone program mix | fixed16 | 0.0–1.0 |
| 2.3 | Speaker level | fixed16 | 0.0–1.0 |
| 2.4 | Input type | int8 | 0 = internal mic, 1 = line, 2 = low mic, 3 = high mic |
| 2.5 | Input levels | fixed16 ×2 | [0] ch0, [1] ch1 |
| 2.6 | Phantom power | boolean | true = powered |

### Category 3 — Output

| Param | Name | Type | Meaning |
|---|---|---|---|
| 3.0 | Overlay enables | uint16 bitfield | bit 0 = status, bit 1 = frame guides (some cameras can't control separately) |
| 3.1 | Frame guides style (Camera 3.x) | int8 | 0 = HDTV, 1 = 4:3, 2 = 2.4:1, 3 = 2.39:1, 4 = 2.35:1, 5 = 1.85:1, 6 = thirds |
| 3.2 | Frame guides opacity (Camera 3.x) | fixed16 | 0.0 = transparent, 1.0 = opaque |
| 3.3 | Overlays (Camera 4.0+) | int8 ×4 | [0] frame-guide style, [1] frame-guide opacity (0–100), [2] safe-area % (0–100), [3] grid style bitfield (thirds/crosshairs/center dot/horizon) |

### Category 4 — Display

| Param | Name | Type | Meaning |
|---|---|---|---|
| 4.0 | Brightness | fixed16 | 0.0–1.0 |
| 4.1 | Exposure and focus tools | int16 bitfield | 0x1 = zebra, 0x2 = focus assist, 0x4 = false color |
| 4.2 | Zebra level | fixed16 | 0.0–1.0 |
| 4.3 | Peaking level | fixed16 | 0.0–1.0 |
| 4.4 | Color bars | int8 | 0 = off, 1–30 = on with timeout (s) |
| 4.5 | Focus Assist | int8 ×2 | [0] method, [1] line color (0 = red, 1 = green, 2 = blue, 3 = white, 4 = black) |
| 4.6 | Program return feed | int8 | 0 = off, 1–30 = on with timeout (s) |

### Category 5 — Tally

| Param | Name | Type | Meaning |
|---|---|---|---|
| 5.0 | Tally brightness | fixed16 | front + rear together, 0.0–1.0 |
| 5.1 | Front tally brightness | fixed16 | 0.0–1.0 |
| 5.2 | Rear tally brightness | fixed16 | 0.0–1.0 (cannot fully turn off) |

### Category 6 — Reference

| Param | Name | Type | Meaning |
|---|---|---|---|
| 6.0 | Source | int8 | 0 = internal, 1 = program, 2 = external |
| 6.1 | Offset | int32 | ± offset in pixels |

### Category 7 — Configuration

| Param | Name | Type | Meaning |
|---|---|---|---|
| 7.0 | Real Time Clock | int32 ×2 | [0] time (BCD), [1] date (BCD YYYYMMDD) |
| 7.1 | System language | string | ISO-639-1 two-char code |
| 7.2 | Timezone | int32 | minutes offset from UTC |
| 7.3 | Location | int64 ×2 | [0] latitude, [1] longitude (BCD) |

### Category 8 — Color Correction

| Param | Name | Type | Range / meaning |
|---|---|---|---|
| 8.0 | Lift Adjust | fixed16 ×4 | [R, G, B, luma], −2.0–2.0, default 0.0 |
| 8.1 | Gamma Adjust | fixed16 ×4 | [R, G, B, luma], −4.0–4.0, default 0.0 |
| 8.2 | Gain Adjust | fixed16 ×4 | [R, G, B, luma], 0.0–16.0, default 1.0 |
| 8.3 | Offset Adjust | fixed16 ×4 | [R, G, B, luma], −8.0–8.0, default 0.0 |
| 8.4 | Contrast Adjust | fixed16 ×2 | [0] pivot (0–1), [1] adjust (0–2) |
| 8.5 | Luma mix | fixed16 | 0.0–1.0, default 1.0 |
| 8.6 | Color Adjust | fixed16 ×2 | [0] hue (−1–1), [1] saturation (0–2) |
| 8.7 | Correction Reset Default | void | reset all of the above |

### Category 9 — (undocumented)

Not in the official spec. Most parameters observed here are ambient
telemetry (~1/s, category-wide, meaning unknown). One exception:

| Param | Name | Type | Meaning |
|---|---|---|---|
| 9.0 | (unknown, ambient ticker) | int16 ×3 (observed) | [sniffer-verified] wire shape only: fires ~1/s in every capture, payload e.g. `9x 2E 64 00 1F 00` — element 0 jitters around a slowly-moving value (and once jumped regime by ~+4300 mid-session, G2 2026-07-27 photo capture, not repeatably correlated with anything); element 1 constant (`100`) in every capture so far; element 2 was constant within earlier sessions but moved during the G2's 2026-07-27 run (`0x19`→`0x1B`→`0x12`→`0x1F`). Meaning unknown. |
| 9.1 | Write-margin warning | int8 (payload offset 1 of 3; offsets 0 and 2 constant/unexplained) | Not [spec] — no official documentation for this category exists. Wire bytes and values are [sniffer-verified]: `1` = nominal, `−2` = low_margin, observed to precede a camera-initiated recording stop by 0.1–1.4s on a known-slow SD card (6/6 occurrences, `POCKET_6K_G2 v7.9` + `POCKET_6K_PRO v8.6`); never `−2` in 7 unrelated normal sessions. The *semantic* attribution to "write speed" specifically is [hypothesis] — not yet isolated from other possible autostop causes (card full, card removed, power loss). Modeled in `payloads/models/*.json`'s `storage.write_margin_warning` (profile provenance status `CANDIDATE`), decoded via `protocol/categories/storage.py` — see `docs/recording.md`'s "Camera-initiated stop detection" section for the full evidence. |
| 9.2 | (unknown — remaining recording time?) | int16 at payload offset 2 of 8 (observed) | [sniffer-verified] wire shape only: fired once after *every* settings change in the 2026-07-20 G2 settings capture, other offsets constant `0x00`. The moving int16's values tracked the new settings in bitrate-consistent order (higher bitrate ⇒ smaller value), so "remaining recording time at current settings" is a live [hypothesis] — needs a varying-card-fill session before it can enter a profile. See `docs/settings.md` §5. New evidence 2026-07-27 (photo captures, both cameras, `docs/photo_capture.md` §5.3): also fires *without* any settings change — once per run on each camera, and on the PRO its value decreased by 1 (`11522`→`11521`) across three stills, consistent with a report-on-change remaining-time value ticking down as card space is consumed. Not per-event: three photos produced one report. Further evidence 2026-07-27 (G2 sensor-area capture, `docs/photo_capture.md` §10.1): fired once per Sensor Area change (a parameter this codebase has no write coordinates for at all) with three genuinely different values, monotonically decreasing as the sensor-area crop widened (2.8K→18620, 5.7K→4791, 6K→3928) — direction consistent with the bitrate-ordered hypothesis, but single-sample per setting and not yet isolated from ordinary drift. **Did not reproduce on `POCKET_6K_PRO v8.6`'s equivalent sensor-area capture the same day** (`docs/photo_capture.md` §10.3): this parameter didn't fire at all in any of that run's three sensor-area windows — a real negative data point weakening (not disproving) the per-setting-correlate reading; could be genuine camera-model difference, an intermittent trigger, or the listen window simply closing before a delayed report. **Firmer negative, 2026-07-27** (`docs/photo_capture.md` §10.4, PRO): a longer-window, interleaved 5-window rerun still produced zero occurrences — 0-for-2 independent PRO sensor-area sessions (8 windows total) despite this same parameter firing for other events on this camera (§5.3's photo-capture session). Reads as camera-consistent absence specifically for sensor-area changes, not an unlucky single miss — the G2's own one-sample sighting can no longer be repeated for comparison, since that camera's firmware has since moved to v8.6. |

### Category 10 — Media ← this project's home turf

| Param | Name | Type | Elements / meaning |
|---|---|---|---|
| 10.0 | Codec | int8 ×2 | [0] basic codec: 0 = CinemaDNG, 1 = DNxHD, 2 = ProRes, 3 = Blackmagic RAW. [1] variant, meaning depends on [0]: CinemaDNG 0 = uncompressed / 1 = lossy 3:1 / 2 = lossy 4:1; ProRes 0 = HQ / 1 = 422 / 2 = LT / 3 = Proxy / 4 = 444 / 5 = 444XQ; Blackmagic RAW 0 = Q0 / 7 = Q1 / 8 = Q3 / 1 = Q5 / 2 = 3:1 / 3 = 5:1 / 4 = 8:1 / 5 = 12:1 (official doc table, operator-provided screenshot, 2026-07-27 — the non-sequential BRAW ordering, Q1/Q3 slotted in at 7/8 after 12:1's 5, is the doc's own numbering, not a transcription error). Camera *reports* on this parameter are [sniffer-verified] on G2 v7.9 (2026-07-20): int8 pair, codec ids ProRes 2 / BRAW 3, variants HQ 0 / 3:1 2 / 5:1 3 observed; both profiles' full BRAW variant tables (including Q1=7/Q3=8) were independently sniffer/operator-confirmed before this screenshot and match it exactly. ProRes 444/444XQ (4/5) and the CinemaDNG/DNxHD basic-codec values (0/1) are new from this doc — neither profile has sniffed or confirmed them; not added to any profile's `codecs` table without that (design principle 6). The *write* stays CANDIDATE external-RE evidence — with the crucial caveat that assigning it does NOT switch the codec family, only the variant; see `docs/settings.md` §1.1, §5. Feeds the `codecs` profile table — sniff per camera |
| 10.1 | Transport mode | int8 ×5+ | [0] mode: 0 = preview, 1 = play, 2 = record; [1] speed: signed, 0 = pause, +1 = 1× forward play, −1 = reverse; [2] flags bitfield: 1<<0 loop, 1<<1 play all, 1<<5 disk1 active, 1<<6 disk2 active, 1<<7 time-lapse recording; [3+] storage medium per slot: 0 = CFast, 1 = SD, 2 = SSD recorder, 3 = USB |
| 10.2 | Playback Control | int8 | clip navigation: 0 = previous, 1 = next |
| 10.3 | Still Capture | void | capture a photo |

10.1 is the parameter behind this repo's sniffer-verified recording
command (category `0x0A`, parameter `0x01`) — see §6. 10.2 remains the
[spec] starting point for the playback target operation, untouched.

10.3 (still capture) is now **confirmed as a write coordinate on both
cameras**: `POCKET_6K_G2 v7.9` (2026-07-27, `docs/photo_capture.md` §7)
and, independently, `POCKET_6K_PRO v8.6` the same day (§9) — a void ASSIGN
to `0x0A/0x03` triggers a real photo on each, verified by inspecting the
SD card's contents on a PC after each send — not by anything observable
over BLE. 10.3 has still never been observed *reported* on the wire, on
either camera, in either direction: not passively (§5 — a body-triggered
still produces no report at all) and not as an echo of the confirmed
write either (every confirmed send's capture window shows only ambient
telemetry). The [spec]'s void typing (the table above) matches the
confirmed write shape exactly, and both cameras agree on the coordinates
and on reserved-byte indifference — the first cross-model data point this
command family has produced.

### Category 11 — PTZ Control

| Param | Name | Type | Meaning |
|---|---|---|---|
| 11.0 | Pan/Tilt Velocity | fixed16 ×2 | [0] pan, [1] tilt; −1.0–1.0 |
| 11.1 | Memory Preset | int8 ×2 | [0] command: 0 = reset, 1 = store, 2 = recall; [1] preset slot (0–5) |

### Category 12 — Metadata

| Param | Name | Type | Meaning |
|---|---|---|---|
| 12.0 | Reel | int16 | 0–999 |
| 12.1 | Scene Tags | int8 ×3 | [0] scene tag, [1] interior/exterior, [2] day (1) / night (0) |
| 12.2 | Scene | string | scene name |
| 12.3 | Take | int8 ×2 | [0] take number (1–99), [1] take tag (−1 = none, 0 = PU, 1 = VFX, 2 = SER) |
| 12.4 | Good Take | void/int8 | mark good take |
| 12.5 | Camera ID | string | up to 29 chars |
| 12.6 | Camera Operator | string | up to 29 chars |
| 12.7 | Director | string | up to 28 chars |
| 12.8 | Project Name | string | up to 29 chars |
| 12.9 | Lens Type | string | up to 56 chars |
| 12.10 | Lens Iris | string | up to 20 chars |
| 12.11 | Lens Focal Length | string | up to 30 chars |
| 12.12 | Lens Distance | string | up to 50 chars |
| 12.13 | Lens Filter | string | up to 30 chars |
| 12.14 | Slate Mode | int8 | 0 = recording, 1 = playback |

Category 12 is the [spec] starting point for the video/photo-metadata
target operation, and `0x0C` ambient telemetry seen on the G2 suggests the
camera streams some of it continuously.

---

## 6. Case study: recording on POCKET_6K_G2 v7.9 — spec vs wire

The one command family fully reverse-engineered so far, and the template
for how [spec] and sniffer evidence combine.

**The command** [sniffer-verified]:

```
FF 05 00 01 0A 01 01 00 02      record start
FF 05 00 01 0A 01 01 00 00      record stop
│  │  │  │  │  │  │  │  └─ payload: 2 = start, 0 = stop
│  │  │  │  │  │  │  └─ operation: ASSIGN
│  │  │  │  │  │  └─ data type: 0x01 (INT8 — see §3)
│  │  │  │  │  └─ parameter: 1
│  │  │  │  └─ category: 10 (Media)
│  │  │  └─ reserved: 0x01  (not 0x00 — but see below)
│  │  └─ command id: 0
│  └─ length: 5 (bytes 4..8)
└─ fixed 0xFF prefix
```

**On the reserved byte** [sniffer-verified]: `0x01` is what this `v7.9`
capture shows, and it is what that profile records — but it is not a
requirement of the family. The `POCKET_6K_G2 v8.6` discovery sweep
(2026-07-29) sent both `reserved=0x00` and `reserved=0x01` for each of
`start` and `stop`, and the camera acted on all four; `0x00` echoed cleanly
for both outcomes and is what that profile records. So the byte is
**indifferent** here, not a value the camera checks — the same conclusion
photo capture reached independently on both cameras
(`docs/photo_capture.md` §7/§9). Read the `(not 0x00!)` above as "don't
assume `0x00` by default, capture it", not as "this family rejects `0x00`".
Per-camera values live in each profile's `commands.recording.reserved`.

Spec alignment: category 10 parameter 1 is Transport mode, whose element
[0] "mode" is `2 = record`, `0 = preview` [spec] — exactly the payload
values captured. So the "start_value 2 / stop_value 0" pair in the profile
is no oddity at all: the command assigns transport **mode** (an int8, per
the spec — matching the captured data-type byte `0x01`), and "stop
recording" really means "return to preview". A
"play" command would plausibly be the same category/parameter with payload
`1` [hypothesis — sniff before use].

**The echo** [sniffer-verified]: one `INCOMING_CONTROL` packet per state
change, operation `0x02` (CAMERA_REPORT), same category/parameter, 6-byte
payload:

```
record start echo payload:  02 00 40 00 01 03
record stop  echo payload:  00 00 40 00 01 03
```

**Transport-mode struct mapping** [hypothesis]: reading that payload as the
spec's 10.1 element layout fits perfectly —

| Byte | Spec element | Captured value | Reading |
|---|---|---|---|
| 0 | mode | `02` / `00` | record / preview |
| 1 | speed | `00` | paused/none |
| 2 | flags | `40` | 1<<6 = "disk 2 active" (SD slot on a G2?) |
| 3 | slot 1 storage medium | `00` | CFast |
| 4 | slot 2 storage medium | `01` | SD |
| 5 | slot 3 storage medium | `03` | USB — the G2 has exactly three media targets (CFast, SD, USB-C), suggesting one medium byte per slot rather than the spec's fixed two |

If confirmed, this single echo carries recording state **and** active-slot
/ media-type information — directly useful for the storage-monitoring
target operation. To confirm or kill it: capture the echo while (a)
recording to CFast instead of SD (byte 2 should flip to `0x20`), and (b)
playing a clip (byte 0 should read `01`). Until then it stays a hypothesis
and none of these byte meanings may enter a profile.

---

## 7. Cross-references

| Topic | Where |
|---|---|
| Packet codec implementation | `src/bmd_ble/protocol/codec.py`, `docs/packet_structure_and_constants.md` |
| Data type enum | `src/bmd_ble/protocol/types.py` |
| BLE UUID constants | `src/bmd_ble/constants.py` |
| Profile JSON structure and schema | `payloads/schema.json`, `docs/payload_profiles.md` |
| Recording command family | `docs/recording.md` |
| Settings families (codec/quality/resolution/FPS) | `docs/settings.md` |
| Echo-based verification | `docs/session_and_verification.md` |
| Capture tooling (passive/active) | `docs/sniffer_capture_engine.md`, `docs/active_camera_control.md`, `docs/command_discovery.md` |
