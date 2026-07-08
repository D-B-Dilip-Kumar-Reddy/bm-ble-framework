# Blackmagic Camera Control Protocol — full reference

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
readable varies per camera — recorded per-profile as
`gap_meta_data.readable` / `device_info_meta_data.readable`
([sniffer-verified]: G2 v7.9 exposes neither; PRO v8.6 exposes both).

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

### 3.1 This repo's enum (`protocol/types.py`)

| Value | Type | Width (bytes) |
|---|---|---|
| 0 | void | 0 |
| 1 | bool | 1 |
| 2 | int8 | 1 |
| 3 | int16 | 2 |
| 4 | int32 | 4 |
| 5 | int64 | 8 |
| 6 | string | variable |
| 7 | fixed16 | 2 |

### 3.2 The official spec's coding — and an open discrepancy

The official document codes data types differently [spec]:

| Code | Type |
|---|---|
| 0 | void / boolean |
| 1 | signed byte (int8) |
| 2 | int16 |
| 3 | int32 |
| 4 | int64 |
| 5 | UTF-8 string |
| 128 | fixed16 (signed 5.11 fixed point) |

**Open question:** the only data-type byte sniffer-verified so far is
`0x01` on the recording command/echo. This repo reads it as `BOOL`; the
official coding reads it as `int8` — and the spec's transport-mode
parameter (§5, 10.1) is indeed int8. Both interpretations decode a 1-byte
payload, so recording behaviour is identical either way, but the enums
*disagree from code 2 upward*. Before trusting any multi-byte parameter
(e.g. white balance int16, shutter angle int32, any fixed16), capture one
and check byte 6 against both tables. If the official coding wins, remap
`protocol/types.py` (and note `fixed16 = 128`, far outside the current
enum). [hypothesis: the official coding is what cameras actually emit]

### 3.3 fixed16

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
| `0x01` | OFFSET | Add the payload to the current value (toggle for booleans) | [spec]; never yet observed over BLE |
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
| 1 | Video | future: codec/quality/resolution/FPS settings |
| 2 | Audio | — |
| 3 | Output | — |
| 4 | Display | — |
| 5 | Tally | — |
| 6 | Reference | — |
| 7 | Configuration | — |
| 8 | Color Correction | — |
| 9 | (undocumented) | ambient ~1/s telemetry observed on G2 v7.9 [sniffer-verified], meaning unknown |
| 10 | Media | `protocol/categories/recording.py` (10.1); future: photo (10.3), playback (10.2) |
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
| 1.0 | Video mode | int8 ×5 | [0] frame rate (24/25/30/50/60), [1] M-rate (0/1), [2] dimensions, [3] interlaced (0/1), [4] colorspace (0 = YUV) |
| 1.1 | Gain (legacy, ≤ Camera 4.9) | int8 | 1–128: 1×/2×/4×/…/128× |
| 1.2 | Manual White Balance | int16 ×2 | [0] color temp (K), [1] tint (−50–50) |
| 1.3 | Set auto WB | void | calculate + set auto white balance |
| 1.4 | Restore auto WB | void | reapply last auto WB |
| 1.5 | Exposure (µs) | int32 | 1–42000 µs |
| 1.6 | Exposure (ordinal) | int16 | steps through available exposures |
| 1.7 | Dynamic Range Mode | int8 | 0 = film, 1 = video, 2 = extended video |
| 1.8 | Sharpening level | int8 | 0 = off, 1 = low, 2 = medium, 3 = high |
| 1.9 | Recording format | int16 ×5 | [0] file frame rate, [1] sensor frame rate, [2] frame width, [3] frame height, [4] flags (file-M-rate, sensor-M-rate, sensor off-speed, interlaced, windowed) — the likely home of this project's resolution/FPS settings |
| 1.10 | Auto exposure mode | int8 | 0 = manual, 1 = iris, 2 = shutter, 3 = iris+shutter, 4 = shutter+iris |
| 1.11 | Shutter angle | int32 | 100–36000 (degrees × 100) |
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

### Category 10 — Media ← this project's home turf

| Param | Name | Type | Elements / meaning |
|---|---|---|---|
| 10.0 | Codec | int8 ×2 | [0] basic codec, [1] variant. BRAW variants: 0 = Q0, 1 = Q5, 2 = 3:1, 3 = 5:1, 4 = 8:1, 5 = 12:1. This is where `codec_ids`/`quality_ids` profile tables will come from — sniff per camera |
| 10.1 | Transport mode | int8 ×5+ | [0] mode: 0 = preview, 1 = play, 2 = record; [1] speed: signed, 0 = pause, +1 = 1× forward play, −1 = reverse; [2] flags bitfield: 1<<0 loop, 1<<1 play all, 1<<5 disk1 active, 1<<6 disk2 active, 1<<7 time-lapse recording; [3+] storage medium per slot: 0 = CFast, 1 = SD, 2 = SSD recorder, 3 = USB |
| 10.2 | Playback Control | int8 | clip navigation: 0 = previous, 1 = next |
| 10.3 | Still Capture | void | capture a photo |

10.1 is the parameter behind this repo's sniffer-verified recording
command (category `0x0A`, parameter `0x01`) — see §6. 10.2/10.3 are the
[spec] starting points for the playback and photo-capture target
operations.

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
│  │  │  │  │  │  └─ data type: 0x01
│  │  │  │  │  └─ parameter: 1
│  │  │  │  └─ category: 10 (Media)
│  │  │  └─ reserved: 0x01  (not 0x00!)
│  │  └─ command id: 0
│  └─ length: 5 (bytes 4..8)
└─ fixed 0xFF prefix
```

Spec alignment: category 10 parameter 1 is Transport mode, whose element
[0] "mode" is `2 = record`, `0 = preview` [spec] — exactly the payload
values captured. So the "start_value 2 / stop_value 0 with data_type BOOL"
oddity in the profile is no oddity at all: the command assigns transport
**mode**, and "stop recording" really means "return to preview". A
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
| Echo-based verification | `docs/session_and_verification.md` |
| Capture tooling (passive/active) | `docs/sniffer_capture_engine.md`, `docs/active_camera_control.md`, `docs/command_discovery.md` |
