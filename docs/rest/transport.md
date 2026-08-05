# REST / WebSocket Transport

**Status:** Phase 0 — swept on both `POCKET_6K_G2 v8.6` and `POCKET_6K_PRO v8.6`, over
USB, 2026-08-03. `PUT /system/format` returns `204` on both — the gate question
deciding whether settings can move to REST at all — plus roughly 30 other endpoints
each, same-value only. The two cameras agree almost everywhere (addressing, scheme,
the mounts `5xx` defect, the `supportedFormats` capability matrix) and differ exactly
where their hardware differs (the PRO has a built-in ND filter the G2 lacks). Full
results under "Sweep results". No REST client, session, or profile exists yet; that is
Phase 2.

## Overview

Firmware 8.6 exposes an HTTP control API and a WebSocket event feed alongside the BLE
interface this repo was built on. Several things BLE structurally cannot do are
straightforward over REST, and one thing BLE does well has no REST equivalent at all.

This doc is the REST counterpart to `docs/ble/protocol.md`: it records what the transport
actually is on real hardware, what each endpoint does on *this* camera and firmware, and
what remains unknown. It is not a restatement of Blackmagic's published specs — those
describe an API; a given camera implements some subset of it, sometimes badly.

### Why the sweep comes before any code

Three defects were already found by hand on `POCKET_6K_G2 v8.6`, all by accident:

| Endpoint | Observed | Meaning |
|---|---|---|
| `GET /system` | `204 No Content` | Documented as returning a `SystemResponse`; returns nothing |
| `GET /system/videoFormat` | `501 Not Implemented` | Not available on this device |
| `GET /system/codecFormat` | `501 Not Implemented` | Not available on this device |
| `GET /mounts/<volume>/Stills/` | `500 Internal Server Error` | **Firmware defect** — the mount root and individual files both work; a second sweep later found this is not Stills-specific — every subdirectory under a mount root 500s |

If three turned up by accident, more will turn up on purpose. So the first deliverable of
the REST work is evidence, not architecture: sweep every endpoint, record what each one
actually returns, and only then decide which BLE functionality can move.

This is the HTTP analogue of design principle 6 — *never copy protocol values from one
profile to another without re-verifying*. A sweep's results are evidence about one
`(model_key, firmware, transport)` triple and nothing else.

---

## The transport is USB, not LAN

The camera presents a **USB Ethernet gadget** and serves the control API over it.

Exact conditions everything in this doc was observed under, recorded because the repo
treats test conditions as provenance: `POCKET_6K_G2 v8.6`, connected to a Windows laptop
by a **USB-C to USB-C cable**. Not over Wi-Fi, not over a shared network, and not through
a hub or an A-to-C cable.

That is the same channel `docs/ble/photo_capture.md` §7.3 proposed as a way out of the
photo-verification deadlock ("`POCKET_6K_PRO v8.6` exposes an HTTP interface over USB
where clips/photos can be browsed and played back from a PC"). This work is that TODO,
on the interface it actually named.

The USB-C port is also the camera's external-media port, but that costs this project
nothing: **the storage scope is the SD card slot**, with CFast possibly added later and
external USB media out of scope entirely (see CLAUDE.md's Storage Media Monitoring
section). So holding the port for the network connection is free.

It does mean the media code must not assume one device. `/media/workingset` reports a
working set and `/media/active` names the active member, so resolving the active device
from those two — rather than indexing slot 0 — makes adding CFast later a data change
instead of a code change.

### Addressing the camera

**Use the mDNS name.** Verified 2026-08-03:

```
Resolve-DnsName pocket-cinema-camera-6k-g2.local  ->  172.30.161.225
```

**The USB-C link is its own /30 point-to-point network.** From `ipconfig`, adapter
"Ethernet 3":

| | Address |
|---|---|
| Camera | `172.30.161.225` |
| Laptop | `172.30.161.226` |
| Netmask | `255.255.255.252` (a /30 — exactly two usable hosts) |
| Gateway | none |

**Do not use the IP from Setup → Network Settings.** That screen reads `10.0.0.3` /
gateway `10.0.0.1`, and connecting to it over USB is *refused*. It describes the
**Ethernet port's** configuration, which is a different interface entirely. Two heuristics
are therefore both wrong on this camera, and `probe_endpoints.py` offers neither: "use the
Setup IP" and "the camera is your adapter's default gateway" (there is no gateway on a
/30).

**Setup → Network Access is the authority on the scheme.** It shows the URL the camera
actually serves:

| Setting | Listener |
|---|---|
| `Web media manager (HTTP): Enabled` | plaintext, port 80 |
| `Web media manager (HTTP): Enabled with security only` | TLS, port 443 |

This camera is set to `Enabled`, and its Setup screen shows an `http://` URL — consistent
with port 443 refusing for both `probe_endpoints.py` and `curl` on 2026-08-03 while the
link itself was demonstrably healthy. The tool defaults to `--scheme http` for that
reason, and the preflight retries with the other scheme before giving up, so the setting
is not something you must know in advance. `--insecure` is needed only on the TLS
listener, whose certificate is self-signed.

### Assumptions this doc got wrong, and what corrected them

Kept rather than quietly edited away, because each was believed on reasonable-looking
evidence and each cost a debugging round.

| Claimed | Actual | Corrected by |
|---|---|---|
| USB means no mDNS | `.local` resolves to `172.30.161.225` | `Resolve-DnsName`, 2026-08-03 |
| SMB fails because the name does not resolve | The name resolves; SMB fails for some unrelated reason | the same |
| The camera is at `10.0.0.3` (Setup → Network Settings) | That is the **Ethernet** port; over USB it is `172.30.161.225` on a /30 | `ipconfig` + `Resolve-DnsName` |
| The camera is the adapter's default gateway | A /30 has no gateway at all | `ipconfig` |
| The API is served over HTTPS | Setup advertises `http://`, and 443 refuses | `curl`, and the camera's own Setup screen |

The last one is worth dwelling on: it came from a browser address bar showing `https://`.
Browsers silently try HTTPS first and fall back, so **the address bar is not evidence of
which scheme a device serves.** The camera's own Setup screen is.

SMB is `Enabled` on the camera and still unreachable from Explorer; FTP is enabled too.
Both are out of scope and neither is used.

### Other consequences that shape the design

- **The link is single-host and disappears** on unplug, camera sleep, or power cycle.
  BLE handles its equivalent with `_reconnect_loop()` and liveness detection
  (`docs/ble/winrt_ble_connection_hardening.md`); REST has no answer for it yet. Whether
  the interface survives a full recording is **unverified** and needs checking before
  anything depends on it.
- **USB results are not LAN results.** `Allow utility administration` is set to
  `via USB and Ethernet`, so both paths are open and the distinction is real rather than
  theoretical. Moving to Wi-Fi means re-running the sweep, for the same reason a new
  firmware means re-sniffing. `probe_endpoints.py` records `--transport usb|lan` in every
  report so a reader can never mistake one for the other.
- **The address is never cached in a profile.** Profiles record per-endpoint behaviour;
  they do not record where the camera lives.

### When nothing is reachable

Two runs reached nothing before the addressing was understood. The failure *shapes*
differed, and that difference was the useful part:

| Address | Per request | Exception | Meaning |
|---|---|---|---|
| `https://10.0.0.3` | ~3 s | `ClientConnectorError: … refused the network connection` | Wrong interface — that is the Ethernet port's IP |
| `https://pocket-cinema-camera-6k-g2.local` | ~11 s | `TimeoutError` | Right host, wrong port — nothing listens on 443 |

A refusal and a hang are different diagnoses, so the tool always prints the exception
rather than a bare "unreachable". `curl` reproduced both failures identically, which is
what ruled out the tool itself as the cause — worth repeating whenever a result looks
like a client bug:

```powershell
Resolve-DnsName pocket-cinema-camera-6k-g2.local   # what the name resolves to
ipconfig                                           # which adapter, which subnet
curl.exe -v http://pocket-cinema-camera-6k-g2.local/control/api/v1/system/format
```

Also worth ruling out: Windows Firewall classifying the adapter as *Public*, and a stale
link — the USB connection drops on sleep, unplug, and power cycle, so a browser tab that
worked earlier is not evidence that it works now.

---

## What the sweep must answer

These questions gate the phases that follow. None of them can be answered from the specs.

| Question | What it decides |
|---|---|
| Does `PUT /system/format` exist (not 501)? | Whether settings can move to REST at all. With `/system/codecFormat` and `/system/videoFormat` both 501, `/system/format` is the **only** format surface — if its PUT is also unimplemented, codec/resolution/fps stay on BLE |
| Does `GET /system/supportedFormats` work? | Whether capability discovery is a runtime query or stays a hand-maintained profile table |
| Which WS properties actually subscribe? | Whether write verification has a real primary channel |
| Is `PUT /transports/0/record` implemented? | Whether record start/stop can move to REST |
| Are the 5xx defects wider than Stills? | How photo confirmation has to be built |
| Does `/clips/list` expose stills, sizes, or timestamps? | Whether photo confirmation needs filename probing at all, or something far simpler |
| Is `sensorResolution` writable via `PUT /system/format`? | Whether REST solves the Sensor Area problem `docs/ble/photo_capture.md` §10 closed as unsolvable over BLE |
| Does `/transports/0/timecode` return decimal or hex-valued BCD? | Whether `timecode.py`'s BCD decode can be reused |
| Does the USB interface stay up across a recording, and while BLE is connected? | Whether REST recording is safe, and whether the hybrid photo path is possible |
| How does a clip's `filePath` map to an HTTP path? | Phase 6's photo confirmation. The operator's samples show `/clips/list` reporting `/mnt/sd0/A001/A001_07311253_C001.mov` while the same card serves as `/mounts/A001-sd1/…` — **`sd0` vs `sd1`**, so the mapping is not string manipulation and must not be guessed |
| Is the plaintext HTTP listener live, or is it HTTPS-only? | Whether `RestClient` must always do TLS, and whether the self-signed certificate has to be pinned or waived in production code |

---

## `tools/rest/probe_endpoints.py`

Standalone and dependency-light: `aiohttp` only, importing nothing from the camera
package, so it runs against the repository exactly as it stands. Nothing has to be
refactored before evidence can be gathered.

Its pure logic — endpoint catalog, path expansion, status classification, summary
rendering, profile-block emission — is unit-tested in
`tests/unit/tools/rest/test_probe_endpoints.py` with no network and no `aiohttp`
installed. Only the functions that actually talk to the camera need the dependency.

### Mode 1: read-only (default)

Changes nothing on the camera; safe to re-run.

- `GET` every path from the ten published control specs, plus `/clips/list` (which no
  uploaded spec covers, but which the operator confirmed works).
- Fill `{deviceName}` from a live `/media/workingset` read. A template with no known
  device names is **dropped**, not requested literally — a 404 for the path
  `/media/devices/{deviceName}` would be a fact about the tool, not the camera.
- `GET /event/list` — the camera's own authoritative list of subscribable WS properties,
  which may differ from `Notification.yaml`'s `x-propertyName` enum. Both sets are tried
  and compared.
- Open the WebSocket and subscribe to each property **individually**. One property per
  request, never a batch: a batch that fails tells you nothing about which member caused
  it. Same reasoning as one capture window per setting in the BLE sniffers.
- Walk the `/mounts/` tree breadth-first, recording each level, and explicitly reach the
  Stills directory so every run either reproduces the known 500 or shows it fixed.

Requests are issued **sequentially**. A camera serving this over a USB gadget is not a
load-test target, and parallel requests would make a failure impossible to attribute.

### Mode 2: `--probe-writes` (opt-in, typed-yes gated)

No `GET` can tell you whether a `PUT` is implemented, and a real `PUT` changes camera
state. The way out is an **idempotent probe**: read the current value, write that exact
value straight back, and record the status code.

- `501` → not implemented.
- `200`/`204` → implemented.

Either answer arrives without changing a single setting.

Several endpoints report more fields than their `PUT` accepts, so the value read is
reshaped before being sent back:

| Endpoint | Reshaping |
|---|---|
| `/video/shutter` | Drops the read-only `continuousShutterAutoExposure`; sends `shutterSpeed` or `shutterAngle` |
| `/lens/iris` | Sends one of `apertureStop` > `normalised` > `apertureNumber`, the spec's documented priority |
| `/lens/zoom` | Sends `focalLength` or `normalised` |
| `/audio/channel/N/level` | Sends `gain` or `normalised` (gain is prioritised) |
| `/media/active` | Sends `workingsetIndex` only |
| `/transports/0` | Echoes `InputPreview`/`Output`; **skips** when the camera reports `InputRecord`, which its PUT cannot accept and which means the camera is mid-take |

Anything that cannot be reshaped into a valid body is skipped rather than guessed at — a
fabricated request would test the tool's imagination, not the camera.

**Never probed, at any value** (`NEVER_WRITE`), because these change state regardless of
what is sent:

```
/media/devices/{deviceName}/doformat   erases the card
/transports/0/record                   starts/stops recording
/transports/0/play                     starts playback
/transports/0/stop                     stops playback
/timelines/0            (DELETE)       clears the timeline
/timelines/0/add        (POST)         mutates the timeline
/lens/focus/doAutoFocus                moves the lens
/video/whiteBalance/doAuto             re-measures white balance
```

A unit test asserts the catalog agrees with that list, so wiring a write builder onto a
destructive endpoint fails in CI rather than on real hardware.

**Honest limitation, recorded in every report:** a same-value PUT may be a camera-side
no-op, so a `200` proves the *endpoint exists*, not that a *changing* write applies.
That second question belongs to the phase that needs it — and the BLE work has a
precedent for exactly this trap (`docs/ble/settings.md` §11, §14: every settings family
goes silent on a redundant write).

### Preflight

Before sweeping anything, one GET to `/system/format` — the operator-confirmed working
endpoint — has to answer. If it does not, the tool prints the exception and an ordered
list of next steps chosen from that exception, then exits. Sweeping 70+ endpoints against
a host that never replies costs minutes and buries the one fact that matters. `--force`
overrides it; that is rarely what you want.

Every log line carries the real exception, not just a classification. An early version
printed only "unreachable", which cannot distinguish a refused connection from a TLS
failure from a DNS miss — the first real run produced 70 identical useless lines because
of it.

### Output

Three artefacts per run:

1. A printed summary grouped by classification, **worst first** — `server_error` above
   `unreachable` above `not_implemented` above `ok`. A 500 buried under forty 200s is a
   500 nobody reads.
2. A raw JSON report under `tools/captures/rest/<MODEL_KEY>_<FIRMWARE>/` (gitignored,
   alongside the BLE captures), carrying full response bodies, latencies, and the
   same-value-PUT caveat.
3. A ready-to-paste REST profile block, the same way
   `tools/control/discover_command.py` emits a `commands` block.

`501` is deliberately classified apart from other `5xx`. The first is the camera
correctly saying "this device does not do that"; the second is a firmware defect.
Conflating them would hide the exact class of problem the sweep exists to find. Only
`ok` counts as `supported` — a `server_error` endpoint is reachable but broken, which is
worse than absent, because at the call site it looks like a transient failure worth
retrying.

The emitted block leaves **`format_names` empty on purpose**. Deriving REST codec strings
(`"BRaw:5_1"`, `"ProRes:HQ"`) needs `/system/supportedFormats`; inventing them from a
naming rule would be exactly the kind of unverified protocol value design principle 1
exists to keep out of this repo.

### Usage

```
python tools/rest/probe_endpoints.py --host pocket-cinema-camera-6k-g2.local \
    --model-key POCKET_6K_G2 --firmware v8.6 --insecure
python tools/rest/probe_endpoints.py --host pocket-cinema-camera-6k-g2.local \
    --model-key POCKET_6K_G2 --firmware v8.6 --insecure --probe-writes
```

`--insecure` is required — the camera's certificate is self-signed. Run without `--host`
to print where on the camera to read the address from. Re-run with `--scheme http` to
find out whether the plaintext listener is live too.

`--model-key` and `--firmware` are **required, with no defaults** — matching the
convention for `tools/control/` scripts, where the target is never implicit because the
results get filed as evidence against it.

Run it **per camera and per transport**. `POCKET_6K_G2 v8.6` over USB says nothing about
`POCKET_6K_PRO v8.6`, even on the same firmware version — the BLE work already has two
profiles on the same firmware diverging on a real protocol value
(`recording_format`'s data-type byte, `0x82` on v7.9 vs `0x02` on v8.6).

---

## Sweep results

### `POCKET_6K_G2 v8.6`, over USB, plaintext HTTP — 2026-08-03

First successful sweep. `http://pocket-cinema-camera-6k-g2.local`, 72 endpoints in
~400 ms. **52 working, 5 not implemented, 18 not found, and — notably — zero 5xx.**

Read this section together with "Known gaps in this run" below: three tool bugs the run
exposed mean parts of it must be re-gathered before being trusted.

#### Not implemented on this device (501)

| Endpoint | Note |
|---|---|
| `/system/codecFormat` | Reconfirms the operator's manual finding |
| `/system/videoFormat` | Reconfirms |
| `/system/supportedCodecFormats` | New |
| `/system/supportedVideoFormats` | New |
| `/video/ndFilter` | Expected — this camera has no built-in ND. `/video/ndFilter/displayMode` returns 204 rather than 501, which is inconsistent but harmless |

All five return a body: `{"error": "Not implemented for this device"}`.

`GET /system` returns **204 No Content**, as before — reachable, implemented, and empty.

#### 404s that are facts about the camera, not failures

`/audio/channel/2/*` and `/audio/channel/3/*` return `{"error": "Channel N does not exist"}`.
The camera has exactly **two** audio channels. The sweep probes 0–3 deliberately, because
the spec never says how many exist.

#### `/system/supportedFormats` — the capability matrix

Working, and it makes the profile's hand-maintained `codecs`/`resolutions`/`fps_modes`
tables redundant on the REST path:

| Record resolution | Sensor resolution | Codec family | Max fps | Max off-speed |
|---|---|---|---|---|
| 1920×1080 | 2880×1512 | ProRes | 60 | 120 |
| 1920×1080 | 5376×3024 | ProRes | 60 | 60 |
| 1920×1080 | 6144×3456 | ProRes | 50 | 50 |
| 2880×1512 | 2880×1512 | BRaw | 60 | 120 |
| 3728×3104 | 3728×3104 | BRaw | 60 | 60 |
| 3840×2160 | 5376×3024 | ProRes | 60 | 60 |
| 3840×2160 | 6144×3456 | ProRes | 50 | 50 |
| **4096×2160** | **5744×3024** | **ProRes** | **60** | 60 |
| 4096×2160 | 4096×2160 | BRaw | 60 | 60 |
| 5744×3024 | 5744×3024 | BRaw | 60 | 60 |
| 6144×2560 | 6144×2560 | BRaw | 60 | 60 |
| 6144×3456 | 6144×3456 | BRaw | 50 | 50 |

Three findings fall straight out of that table.

**1. ProRes at 4K DCI is supported — and the camera is currently in it.**
`GET /system/format` reports `codec: ProRes:Proxy`, `recordResolution: 4096×2160`. That is
precisely the combination BLE records as `known_unreachable` on this same firmware
(`docs/ble/settings.md` §18.12), after nine falsification attempts across three sessions.
This **independently vindicates that entry's central claim** — quoted from the profile:
"This is a software capability gap in this codebase's BLE write path, not evidence the
camera-side combination is unsupported." The camera reaches and holds it; only the BLE
write path could not get there. Note also its sensor resolution is **5744×3024**, which no
BLE-side field ever captured.

**2. The 6K fps ceiling is confirmed from a second, independent transport.**
6144×3456 offers frame rates up to `50` only, while 6144×2560 (6K 2.4:1) offers `59.94`
and `60`. That is exactly `resolutions."6K".max_fps_int = 50`, derived on BLE from a 16/16
sweep failure plus an operator UI check. Two transports, same answer.

**3. `sensorResolution` is the Sensor Area concept, exposed as a first-class field.**
ProRes at 1920×1080 appears three times, differing only in sensor resolution: 2880×1512,
5376×3024, 6144×3456. That is the 2.8K / 5.3K / 6K "Sensor Area" selector that
`docs/ble/photo_capture.md` §§10–10.7 closed as having **no BLE write path by any means
tried** — read-only through one flag bit, never writable, and absent from the official
115-page spec. REST at minimum *enumerates* it. Whether `PUT /system/format` can *change*
it is the single highest-value question left for the write probe.

#### Codec naming

| | Values |
|---|---|
| REST ProRes | `Proxy`, `LT`, **`Original`**, `HQ` |
| BLE profile ProRes | `Proxy`, `LT`, **`422`**, `HQ` |
| REST BRaw | `Q0`, `Q1`, `Q3`, `Q5`, `3_1`, `5_1`, `8_1`, `12_1` |
| BLE profile BRAW | `Q0`, `Q1`, `Q3`, `Q5`, `3:1`, `5:1`, `8:1`, `12:1` |

BRaw maps by a rule (`:` → `_`). **ProRes does not**: REST calls the variant `Original`
where this repo calls it `422`. That single exception is exactly why `format_names` belongs
in the profile rather than being derived in code (design principle 1).

#### Storage — and why "don't index slot 0" was right

`/media/workingset` reports `size: 3` with only **index 1** populated:

```json
{"index": 0, "deviceName": "",    "activeDisk": false, "totalSpace": 0}
{"index": 1, "deviceName": "sd0", "activeDisk": true,  "volume": "A001",
 "clipCount": 1, "remainingRecordTime": 52233, "remainingSpace": 1023925420032}
{"index": 2, "deviceName": "",    "activeDisk": false, "totalSpace": 0}
```

The working set is a **fixed-size array with empty members**, and the active disk is not
slot 0. Any code indexing `workingset[0]` would find an empty device on this camera today.
`/media/active` names it directly (`{"deviceName": "sd0", "workingsetIndex": 1}`) and
`/media/devices/sd0` reports `{"state": "Mounted"}`.

Everything design principle 10 needs is here: card presence, state, free space, remaining
record time, clip count.

#### The clip-path mapping is still not string manipulation

`/clips/list` returns `clipList` (not `clips`, as assumed):

```json
{"clipUniqueId": 1, "filePath": "/mnt/sd0/A001/A001_07311253_C001.mov",
 "codecFormat": {"codec": "ProRes:Proxy", "container": "MOV"},
 "startTimecode": "12:53:56:01", "durationTimecode": "00:00:02:12",
 "videoFormat": "4096x2160p24"}
```

Three names for one card: `deviceName` **`sd0`**, `volume` **`A001`**, mount
**`A001-sd1`** — note `sd0` against `sd1`. The mapping from a clip's `filePath` to a
downloadable `/mounts/...` URL still cannot be guessed.

**No stills appear in `clipList`, and there is no file size field.** So the photo
confirmation design cannot lean on `/clips/list`; it still needs the mounts route.

#### Timecode is BCD, big-endian, and byte-reversed relative to BLE

`GET /transports/0/timecode` returned `{"clip": 0, "timecode": 274153986}`.

```
274153986 = 0x10574202 -> 10:57:42:02
```

The sweep ran at 10:57:46 local, so this is time-of-day timecode as **BCD HH:MM:SS:FF,
big-endian**. The BLE `TIMECODE` characteristic decodes as `[frames, seconds, minutes,
hours]` — **the opposite order** (`docs/ble/timecode.md`). This sweep's `clip` value was
just `0` and wasn't investigated further at the time; the official `Notification.yaml`
spec later confirmed it's BCD-encoded in this exact same format (position within the
current clip, not time-of-day) — see `RestCameraSession.clip_timecode()` and
`docs/rest/session.md` for the decode confirmation against real recorded data. The
`Timecode` dataclass and
`duration_seconds()` are reusable; `decode_timecode()` is not.

#### Undocumented properties `/event/list` revealed

`/event/list` returned 46 subscribable properties, three of which appear in **no uploaded
spec**: `/camera/id`, `/presets`, `/presets/active`. Subscribing returned `{"id": 1}`,
`{"presets": []}` and `{"preset": "default"}` respectively. Worth a look if camera
identity or preset recall ever matters.

`/system/codecFormat` and `/system/videoFormat` are rejected by the event feed too —
`"Cannot subscribe to unknown property"` — consistent with their 501s.

### Known gaps in the first run, and their re-run outcome

Three tool bugs were found in the first run. All three are confirmed fixed by a
second sweep run minutes later (`--mounts-depth 3`, otherwise identical), which is the
run the results above are drawn from:

| Bug | First run | Second run |
|---|---|---|
| WS responses matched positionally instead of by `id` | Every result shifted by one | **Fixed** — 46/49 succeeded; the 3 failures are exactly `/system/codecFormat`, `/system/videoFormat`, `/system/supportedFormats`, each self-referentially rejected as `"Cannot subscribe to unknown property '<its own path>'"` — consistent with their `501`s, and no longer attributed to the wrong property |
| `/mounts/` listing assumed HTML, is JSON | Walk never descended past `/mounts/` | **Fixed** — descended 3 levels, reached both subdirectories |
| Empty working-set slots expanded into requests | Two meaningless 404s | **Fixed** — `Media devices discovered: sd0` only |

### The 500 is not Stills-specific — it is every subdirectory

This is the one substantive change from re-running with a working mounts walk.
`GET /mounts/A001-sd1/` — the **mount root** — works and returns full metadata:

```json
[
  {"name": "Stills", "type": "directory", "mtime": "Fri, 31 Jul 2026 12:54:20"},
  {"name": "A001_07311253_C001.mov", "type": "file",
   "mtime": "Fri, 31 Jul 2026 12:53:58", "size": 49058872},
  {"name": "System Volume Information", "type": "directory", "mtime": "..."}
]
```

**Every directory one level below it returns 500** — `Stills/` and, newly discovered,
`System Volume Information/` too:

```
GET /mounts/A001-sd1/Stills/                       -> 500
GET /mounts/A001-sd1/System Volume Information/    -> 500
```

So the defect is not "Stills doesn't list" — it's **"no subdirectory under a mount root
lists."** The mount root itself is fine and gives real metadata (name, type, mtime, and
**size** for files); one level down, the listing endpoint breaks unconditionally.

This matters directly for Phase 6. **Clips live in the mount root, not a subdirectory** —
`A001_07311253_C001.mov` sits directly under `/mounts/A001-sd1/`, with a real `size`
field. A clip could in principle be confirmed by watching the root listing's size or
entry count change. **Stills apparently go into the one kind of location that never
lists**, so the root-listing route does not extend to them; the filename-probing design
in Phase 6 still stands as written.

### The clip-path mapping — a pattern, not yet a rule

`/clips/list` reports `filePath: /mnt/sd0/A001/A001_07311253_C001.mov`. The mount root
that actually serves that file over HTTP is `/mounts/A001-sd1/`. Line up the pieces:

| Internal `filePath` | HTTP mount |
|---|---|
| `/mnt/` prefix | dropped |
| `sd0` (`deviceName`) | `sd1` in the mount name |
| `A001` (reel folder) | folded into the mount name: `A001-sd1` |
| `A001_07311253_C001.mov` | same, directly under the mount root |

So `A001-sd1` is `<reel>-<slot-label>`, and the reel subdirectory the internal path shows
does **not** reappear under `/mounts/` — the mount root **is** the reel folder. One
plausible read of `sd0` → `sd1`: `deviceName` is 0-indexed while the physical slot label
is 1-indexed (`workingsetIndex` was `1` for this same device) — a hypothesis, not
confirmed, and not something to encode as a rule without a second reel or a second slot
to test it against. The gate-table question stays open; what changed is that "not string
manipulation" now has a concrete example to design against instead of being asserted in
the abstract.

### Gate table status

Answered from the `POCKET_6K_G2 v8.6` sweep. The `POCKET_6K_PRO v8.6` sweep below
reconfirmed every one of these identically except where the cameras' hardware genuinely
differs (the ND filter) — see that section rather than a second copy of this table.

| Question | Answer |
|---|---|
| `GET /system/supportedFormats` works? | **Yes** — full capability matrix, above |
| Plaintext HTTP listener live? | **Yes** — port 80, `Server: BlackmagicDesign` |
| `/clips/list` exposes stills or sizes? | **No size field on clips either** — but the mount root listing does (`size: 49058872` on the one `.mov` present), so a clip's own directory entry can confirm it, just not via `/clips/list` |
| Timecode encoding? | **BCD, big-endian HH:MM:SS:FF**, reversed vs BLE |
| Are the 5xx defects wider than Stills? | **Yes — every subdirectory under a mount root 500s**, not Stills specifically |
| Which WS properties subscribe? | **46/49** — every property except the three `501` `/system/*` endpoints, which the event feed also rejects as unknown |
| `PUT /system/format` implemented? | **Yes — 204**, same-value probe |
| `PUT /transports/0/record` implemented? | **Yes** — never probed by this tool (`NEVER_WRITE`), but confirmed 6/6 (3/3 per camera) by `RestCameraSession.record_start`/`record_stop`'s own dual-check verification, `POCKET_6K_G2` and `POCKET_6K_PRO v8.6`, 2026-08-03 — see docs/rest/session.md |
| `sensorResolution` writable? | **Partially answered, 2026-08-03** — `RestCameraSession.set_camera_format` (Phase 5, docs/rest/session.md) now sets it on every write, derived from whichever `sensorResolution` `GET /system/supportedFormats` pairs with the requested `(codec, recordResolution, fps)`, after real hardware showed *not* setting it (i.e. carrying over a prior codec's stale value) gets a `400 {"error": "Format is not supported"}` on a cross-codec switch. That confirms the field must track the pairing to keep a write valid — it does **not** confirm the camera accepts a sensor resolution *other than* one already paired with the requested codec/resolution/fps (the original "Sensor Area selector" question); that remains untried and needs a dedicated probe |
| USB link survives recording, and BLE concurrently? | **Partially answered** — the USB link survived 3 full record/stop cycles on each camera (2026-08-03, no BLE session open concurrently). Concurrent BLE + REST during a recording is still untested |

### Write probe results — `POCKET_6K_G2 v8.6`, over USB, plaintext HTTP — 2026-08-03

Third sweep run, `--probe-writes`, typed-yes confirmed. **30 of 32 write-probeable
endpoints came back `204`.** Only two were skipped, both for the reason the tool prints
rather than a firmware refusal — see below.

This settles the highest-stakes open question from Phase 0: **`PUT /system/format`
returns `204`.** Format writes are not gated on firmware support anymore — the same-value
body sent was the camera's own `GET /system/format` echoed back (`ProRes:Proxy`, `24fps`,
`4096×2160`, `sensorResolution` `5744×3024`). That proves the endpoint exists and accepts
a request shaped like the camera's own report; it does **not** prove a *changing* value
is accepted — see the caveat every report carries, and Phase 5 still needs a real retarget
before anything is built on this.

Also confirmed writable, same-value only: `/transports/0` (mode), `/transports/0/playback`,
`/media/active`, every probed `/video/*`, `/lens/iris`, `/lens/zoom`, all seven
`/colorCorrection/*` endpoints, and every populated `/audio/channel/N/*` field. Nothing
came back `501` or `5xx` on write. This is a far larger confirmed-writable surface than
Phase 0 needed to answer just the gate table — most of the "new capability" list in this
doc's own "New capability REST brings" section is now write-confirmed, not just
read-confirmed.

**Two endpoints were skipped, and both are informative, not failures:**

- **`/video/ndFilter/displayMode`** — its `GET` returns `204 No Content` (no ND filter on
  this camera, consistent with `/video/ndFilter` itself being `501`). There is nothing to
  echo back, so the idempotent-probe method cannot reach this endpoint at all. A genuine
  limitation of the method, not evidence either way about the PUT.
- **`/lens/focus`** — `LensControl.yaml` documents `GET` returning `{"focus": <normalised>}`;
  the real camera returns **`{"normalised": <value>}`** instead. The write catalog's
  builder looked for the documented key, found nothing, and silently skipped — the
  *correct* behaviour for a genuinely absent field, but for the wrong reason: the field
  wasn't absent, it was named differently than the spec says. **Fixed** in the catalog
  (`pick_first("normalised", "focus")`, real evidence first) with a regression test
  pinning the real payload; a re-run will now probe this endpoint's write too.

No camera setting changed as a result of this run — every write sent back exactly the
value just read.

### `POCKET_6K_PRO v8.6`, over USB, plaintext HTTP — 2026-08-03

Swept with the same tool, same transport, same `--scheme http` default — deliberately not
assumed to transfer from the G2 (design principle 6), and it did not transfer identically.
`pocket-cinema-camera-6k-pro.local` resolved and answered on port 80 exactly like the G2,
confirming the addressing story is not a G2 quirk.

**Read sweep:** 54 working, 4 `501`, 2 `5xx`, 16 `404` — one fewer `501` than the G2.
**Write probe:** all 32 write-probeable endpoints returned `204` — better than the G2's
30/32, and both G2 gaps are explained rather than reproduced:

| Endpoint | G2 v8.6 | PRO v8.6 | Why |
|---|---|---|---|
| `/video/ndFilter` | `501` | **`200`**, `{"stop": 0.0}` | The 6K Pro has a **built-in ND filter**; the G2 does not. A genuine hardware capability difference, correctly surfaced by the sweep — not a bug on either camera |
| `/video/ndFilter/displayMode` | `200`/`204`, no body, write skipped | **`200`**, `{"displayMode": "Number"}`, write **succeeded** | Only echoable when the parent feature exists |
| `/lens/focus` write | skipped, then fixed (previous commit) | **`204`** first time | Confirms the `pick_first("normalised", "focus")` fix generalises — the real field name is `normalised` on this camera too, not a G2 coincidence |

Everything else matches the G2 run: the same `/system/*` `501`s, the same mounts
`5xx` pattern (root and `A001-sd1/` list cleanly; `Stills/` and `System Volume
Information/` both `500`) — reproduced on **different hardware**, which is stronger
evidence that this is a shared 8.6 Web Media Manager defect than a second run on the
same camera could be. The same three `501` endpoints reject the WS subscription
(`48/51`, not `46/49`, purely because of the two extra ND-filter properties).

**`/system/supportedFormats` is structurally identical to the G2's** — the same twelve
`(codec, resolution)` combinations, the same frame-rate ceilings, including `6K` capped
at `50` while `6K 2.4:1` reaches `60`. Independently confirmed on this camera's own
connection, not copied from the G2 run — and itself informative: it suggests the 6K G2
and 6K Pro share the same sensor and recording pipeline, differing in body features
(ND filter, physical controls) rather than recording capability. Two BLE findings
originally established *on this camera* (`docs/ble/settings.md` §16, §17.1) now have a third
independent confirmation from a channel the original BLE work never had — see the
addenda in those sections and in `payloads/models/POCKET_6K_PRO_v8.6.json`.

**Timecode decode reconfirmed on a second camera model.** `289886744` → `11:47:52:18`
and `289886977` → `11:47:53:01`, both matching the sweep's wall-clock time — the fourth
and fifth confirmations of BCD big-endian `HH:MM:SS:FF`, and the first on hardware other
than the G2.

**The clip data is byte-identical to the G2's run** — same filename
(`A001_07311253_C001.mov`), same size (`49058872`), same timestamps, same
`remainingSpace`/`totalSpace` (`1023925420032` / `1024060293120`). This is not a
coincidence worth trusting as independent evidence about either camera: it strongly
suggests the **same physical SD card** was moved between the two camera bodies for this
test. One number does differ meaningfully — `remainingRecordTime` is `52233` on the G2
(active format ProRes/4096×2160/24fps) versus `212102` on the PRO (ProRes/1920×1080/
23.98fps) — consistent with the same free space yielding a longer estimate at a lower
resolution, which is itself a small sanity check that `remainingRecordTime` really is
derived from the active format rather than being a static card property.

No tool bugs found in this run. No camera setting changed.

## What has no REST equivalent

- **Photo capture trigger.** None of the ten specs contains a stills-capture endpoint;
  `TransportControl.yaml` covers record, play, stop, and playback only. The BLE trigger
  (`0x0A`/`0x03`, VOID — `docs/ble/photo_capture.md` §7, §9) stays. What REST can supply
  is the *confirmation* that BLE never had.
- **Camera slate metadata** (BLE category `0x0C`: Reel, Scene, Take, Camera ID, Operator,
  Director, Project). No MetadataControl spec was supplied. Unknown, not absent — the
  sweep answers it.
- **Untethered operation.** BLE needs nothing plugged in; REST currently needs the cable.
  This is the reason the BLE stack stays functional rather than being retired.
- **"IF CARD DROPS FRAME" policy (`Alert` / `Stop Recording`) and any dropped-frame
  count.** Body-menu-only setting, confirmed absent from this profile's REST surface —
  checked against all 76 swept endpoints and all 46 `websocket_properties`, no
  `dropFrame`/`timelapse`/`detailSharpening`/`recordLutToClip` path of any kind (the
  other fields on that same settings page). Real-hardware-confirmed, `POCKET_6K_G2 v8.6`,
  2026-08-05: recording `BRAW 3:1`/`6K` above 23.98fps triggered the camera's own
  `Stop Recording` policy twice, ~2.5-2.8s in each time. No signal reports the drop
  itself, but when the policy is `Stop Recording`, its *consequence* is already fully
  observable — `RestCameraSession.is_recording`/`wait_while_recording()` caught both
  auto-stops correctly with no code changes. A third run with the policy switched to
  `Alert`, same demanding combination, recorded the full 300s clean — `record_stop`
  confirmed normally (not a no-op), and the entire 302-second, 46-property event feed
  shows nothing outside the routine periodic set. `Alert` mode gives this codebase
  nothing to detect a drop with, confirmed by direct observation of the full feed. See
  docs/rest/session.md's `is_recording`/`wait_while_recording()` section for the full
  write-up, including a real edge case (the faster of the two auto-stops left no listed
  clip in `clips()` at all).

## New capability REST brings

Beyond replacing BLE functionality, the specs describe control surfaces this repo has
never had: ISO, gain, white balance and tint, shutter, ND filter, auto-exposure; lens
iris, zoom, focus and autofocus; per-channel audio input, level, phantom power, padding,
low-cut filter; the full DaVinci-style colour corrector; and playback with a real
timeline. All of it unverified until swept.

**Playback and gallery (Phase 7, docs/rest/session.md) is built and now
real-hardware-confirmed end to end on both cameras** —
`RestCameraSession.select_clip()`/`enter_playback()`/`exit_playback()`/`play()`/`pause()`/
`stop()`/`shuttle()`/`seek()` — though it started from a thinner evidentiary base than any
earlier phase. `/transports/0` and `/transports/0/playback` both have a real same-value-`PUT`-`204`
from this doc's own write probe (above); `/timelines/0` and `/timelines/0/add` are in the
`NEVER_WRITE` list and have never had their write side exercised at all until Phase 7's own
code runs — the same position `/transports/0/record` was in before Phase 4. See
docs/rest/session.md's Phase 7 section for which parts of the request/response shapes are
sweep-confirmed and which are this migration's own plan-derived hypotheses.

That first real-hardware run (`POCKET_6K_G2 v8.6`, 2026-08-04, against the original
`set_timeline(clip_unique_ids: list[int])` design) answered both `NEVER_WRITE` unknowns in
quick succession — neither answer was the happy path. `DELETE /timelines/0` returns `501
Not Implemented` — the DELETE method specifically, not the resource generally, since this
doc's own sweep never probed it (DELETE was excluded as destructive, not merely because it
might be unsupported). The fix: catch that specific `BMDUnsupportedError`, log a warning,
and proceed straight to `POST /timelines/0/add`. That `POST`, in its original
one-request-per-clip form with a bare `{"clipUniqueId": id}` body, was then rejected
outright: `400 {"error": "Invalid clips data"}`. Reading "clips" in the error as naming the
required top-level key, the fix: send a single `POST` carrying every clip under `"clips"`
— reusing the shape `GET /timelines/0`'s own confirmed response already carries, per
`_parse_timeline_clip_ids()`.

**Third real-hardware evidence, `POCKET_6K_PRO v8.6`, same day, gathered by operator
testing directly (`PUT`/`GET` by hand, not through this repo's own tooling):**
`/transports/0/playback`'s real body is `{"type": "Play", "loop": bool,
"singleClip": bool, "speed": float, "position": int}` — richer than the migration plan's
`"speed"`-only guess, and it disproves two more of the plan's hypotheses outright: `type`
was `"Play"` for both a paused view (`speed=0.0`) and normal playback (`speed=1.0`), never
`"Shuttle"`/`"Jog"` as the plan guessed, and the position field is `"position"` (an integer
frame count), not `"timecode"`/`"clip"` borrowed from `/transports/0/timecode`.
`RestCameraSession.shuttle()`/`seek()`/`_put_playback()` are rewritten around this real
shape as a read-modify-write, matching `set_camera_format`'s own merge discipline; `seek()`
now takes `position` instead of `timecode`/`clip`. The same session also surfaced an
operational precondition observed directly on the camera body: a clip only plays when the
camera's current codec/quality/resolution/fps matches the clip's own recorded format — see
docs/rest/session.md's `enter_playback()` section.

**Fourth real-hardware evidence, `POCKET_6K_PRO v8.6`, same day, isolated via direct
Postman requests against `/timelines/0`/`/timelines/0/add`:** the second finding's
`{"clips": [{"clipUniqueId": id}]}` body is confirmed the only one of five tried that the
camera accepts at all — `{"clips": [id]}` and `{"clipUniqueIds": [id]}` both return
`{"error": "Not implemented for this device"}`, `{"clip": {"clipUniqueId": id}}`
reproduces the earlier `400`, and `PUT /timelines/0` (tried as a possible full-replace
alternative to the broken `DELETE`) is `405 Method Not Allowed`. But the accepted shape's
`204` doesn't mean applied: `POST`ing `{"clips": [{"clipUniqueId": 1}]}` against a timeline
already holding a different clip (id `12`) returned `204`, and `GET /timelines/0`
immediately after still reported only `[12]` — unchanged. `GET /timelines/0`'s real body
was also captured directly for the first time: `{"clips": [{"clipUniqueId": int,
"frameCount": int}]}`, confirming `_parse_timeline_clip_ids()`'s dict-list branch and its
extra-field tolerance.

**Fifth real-hardware evidence, same session, the one that forced a redesign:** clip `1`
(`ProRes:Proxy`, `4096x2160p24`) didn't match clip `12`'s format (`BRaw:Q3`,
`6144x2560p60`) — the operator switched the camera to `ProRes:Proxy @ 4096x2160p24`
(confirmed via `GET /system/format` immediately beforehand) and re-`POST`ed
`{"clips": [{"clipUniqueId": 1}]}`. The result was **seven** clips —
`[10, 1, 9, 8, 7, 5, 6]`, every clip on the card sharing that exact format, not just clip
`1`. Repeating the identical request with `clipUniqueId: 5` instead of `1` (format
re-verified unchanged, no camera-body interaction in between) produced the *identical*
seven-clip set. Independently, the camera's own on-screen playback view (photographed
live) showed `"CLIP 1/7"` for the same group, confirming this is native camera behavior,
not a REST-API-specific quirk. **The camera has no concept of a caller-curated playlist —
the timeline is always every clip matching the camera's current format, and the requested
`clipUniqueId` does not select which clips are in it.**

That finding retired `set_timeline(clip_unique_ids: list[int])` outright rather than
prompting a fifth patch to it — no request-body fix could make a caller-curated subset
real when the camera itself has no such concept. `RestCameraSession.select_clip
(clip_unique_id: int)` replaces it: pick one clip, switch format to match it if needed
(reusing `set_camera_format`, Phase 5, via a new reverse `Clip` -> profile-vocabulary
mapping — `mapping.py`'s `resolve_ble_codec_name` plus a new pixel-dimension reverse
lookup), then confirm the requested clip is a *member* of whatever `GET /timelines/0`
reports rather than its sole entry.

**Sixth real-hardware evidence, `POCKET_6K_G2` and `POCKET_6K_PRO v8.6`, 2026-08-04, the
first run of `select_clip()` itself:** `python examples/rest_playback.py` ran the complete
Phase 7 sequence — `select_clip()` -> `enter_playback()` -> `play()` -> `pause()` ->
`seek(0)` -> `shuttle(2.0)` forward -> `shuttle(-1.0)` backward -> `stop()` ->
`exit_playback()` — start to finish on **both** cameras, every step's own dual-check
passing. This confirms the combination the fifth finding left untested: reverse-mapping a
clip's format, switching to it via `set_camera_format()`, and syncing the timeline via
`DELETE`-then-`POST` all compose correctly through `select_clip()` on real hardware — at
least for the tested case, where the requested clip's format already matched the camera's
current setting on both runs, so this run doesn't cover the `set_camera_format()` branch
specifically (that piece's own evidence is still Phase 5's). `shuttle()`'s `2.0`/`-1.0`
magnitudes and `seek(0)` are real-hardware-confirmed for the first time here too. See
docs/rest/session.md's `select_clip()` section for the full trail across all findings and
what remains open (a clip whose format doesn't match, forcing the switch; and whether
skipping the DELETE clear leaves stale entries, still untested since this card only ever
held one format group at a time).

**Seventh real-hardware evidence, `POCKET_6K_G2 v8.6`, 2026-08-04, the run that finally
exercised `select_clip()`'s format-switch branch — and surfaced something unexpected:**
with `examples/rest_playback.py` now bracketing its run with `GET /system/format`
snapshots, this run's camera started at `BRaw:8_1 @ 4096x2160p29.97`; `select_clip()`
correctly detected the mismatch against the requested clip's own `BRaw:5_1 @
6144x3456p25`, logged it, and switched format via `set_camera_format()` — the first real
confirmation of that branch (the sixth finding's run never exercised it, since its clip
already matched). But the run's closing snapshot, taken after `stop()`/`exit_playback()`,
reported the format back at the *original* `BRaw:8_1 @ 4096x2160p29.97` — a switch nothing
in the script explicitly requested. `stop()` and `exit_playback()` send the identical `PUT`
right now (`stop()` is a direct alias), so this one run can't attribute the revert to
either specifically, or rule out some other cause tied to the same sequence. Leading,
unconfirmed hypothesis: leaving playback mode (`PUT /transports/0 {"mode":
"InputPreview"}`) restores whatever format preceded `select_clip()`'s switch. See
docs/rest/session.md's `enter_playback()` / `exit_playback()` section for the full
writeup and the suggested follow-up test (call `select_clip()` alone, skip the playback
steps entirely, to see whether the switch itself — not anything playback-related — is
what triggers it).

**Eighth real-hardware evidence, `POCKET_6K_G2 v8.6`, 2026-08-04, the very next run — the
narrowing test from the seventh finding:** `select_clip()` called alone, with
`enter_playback()`/`play()`/`pause()`/`seek()`/`shuttle()`/`stop()`/`exit_playback()` all
skipped entirely, left the camera at the switched format (`BRaw:5_1 @ 6144x3456p25`) — no
revert. This rules out `select_clip()`/`set_camera_format()` themselves as the cause of
the seventh finding's revert: whatever triggers it requires actually entering or leaving
playback mode, not merely switching format. Still not isolated to a single call within
that sequence — `enter_playback()`, `play()`, `pause()`, `seek()`, `shuttle()`, `stop()`,
and `exit_playback()` all remain candidates (`stop()` is `exit_playback()` right now, so
those two still can't be told apart). Next narrowing test:
`select_clip()` + `enter_playback()` + immediate `exit_playback()`, skipping the
play/pause/seek/shuttle steps, to check whether the bare `Output`/`InputPreview` round
trip alone reproduces it.

**Ninth real-hardware evidence, `POCKET_6K_G2 v8.6`, 2026-08-04, the next run — the
eighth finding's own narrowing test:** `select_clip()` + `enter_playback()` + immediate
`exit_playback()`, with `play()`/`pause()`/`seek()`/`shuttle()`/`stop()` all skipped,
reverted anyway — a fresh switch from `ProRes:HQ @ 4096x2160p25` to the requested clip's
`BRaw:5_1 @ 6144x3456p25`, then back to `ProRes:HQ @ 4096x2160p25` right after
`exit_playback()`. This rules out `play()`/`pause()`/`seek()`/`shuttle()`/`stop()` as
necessary triggers — the bare `Output`/`InputPreview` round trip alone reproduces it. Only
`enter_playback()` and `exit_playback()` remain candidates, still not distinguished from
each other since both ran in every reverting test so far. Final narrowing test:
`select_clip()` + `enter_playback()` alone, checking the format *before* calling
`exit_playback()` at all — pins the trigger on whichever side of that boundary the format
is still switched vs. already reverted.

**Tenth real-hardware evidence, `POCKET_6K_G2 v8.6`, 2026-08-04, the ninth finding's own
final narrowing test — and the answer:** `select_clip()` + `enter_playback()` alone, no
`exit_playback()` call at all, left the camera at the switched format (`BRaw:5_1 @
6144x3456p25`) — no revert. Combined with the eighth and ninth findings, this fully
isolates the cause: **`exit_playback()` — leaving playback mode via
`PUT /transports/0 {"mode": "InputPreview"}` — is what reverts the camera's format to
whatever preceded entry into `Output` mode.** `select_clip()`, `set_camera_format()`, and
`enter_playback()` are all ruled out; `stop()` triggers the same revert since it's a
direct alias for `exit_playback()` right now. One caveat: `select_clip()` was the only
format-changing call in every test, so "reverts to pre-`select_clip()`" and "reverts to
whatever preceded `Output` mode" remain indistinguishable from this evidence — they're the
same value in every run so far. `exit_playback()` does not compensate for this or expose
an opt-out; a caller needing a specific format after playback must call
`set_camera_format()` again explicitly. See docs/rest/session.md's `enter_playback()` /
`exit_playback()` section for the full four-run trail.

**Eleventh real-hardware evidence, `POCKET_6K_G2 v8.6`, 2026-08-04, closing `select_clip()`
finding #1's last open question:** `examples/check_timeline_stale_entries.py` switched
between two clips of different formats — `ProRes:Proxy @ 4096x2160p23.98` and
`BRaw:8_1 @ 6144x2560p23.98` — via `select_clip()`, reading `timeline_clip_ids()`
(`GET /timelines/0`) after each switch, in both directions (A -> B, then the reverse).
Both runs: the readback after the second switch contained *only* the newly-selected
clip's own format group — nothing left over from the one before it.
**`POST /timelines/0/add` fully replaces the timeline's contents on this firmware, even
though `DELETE /timelines/0` never runs (`501`).** This closes the one open question
`select_clip()`'s design has carried since its first real-hardware run. See
docs/rest/session.md's `timeline_clip_ids()` and `select_clip()` sections for the full
writeup.

---

## Library surface (Phase 2)

Everything above was the evidence phase (Phase 0). Phase 2 turned it into the
transport-only client layer `RestCameraSession` (Phase 3, see docs/rest/session.md) sits
on top of — `src/bmd_camera/rest/`: `constants.py`, `client.py`, `events.py`,
`exceptions.py`. No camera semantics live here, mirroring the boundary
`camera_controller.py` holds for BLE (design principle 5).

### `RestClient` (`src/bmd_camera/rest/client.py`)

Thin async wrapper over `aiohttp`. Its whole job is turning this camera's actual status
codes into the right exception — a naive `raise_for_status()` gets two of them wrong,
both confirmed by the Phase 0 sweep:

| Status | Meaning here | `RestClient` behaviour |
|---|---|---|
| `204` | Accepted, empty body (`GET /system`, most successful `PUT`s) | Returns `None` |
| `2xx` with a body | Success | Returns the parsed JSON |
| `501` | Camera correctly declining — not implemented on this device | Raises `BMDUnsupportedError` (design principle 7) |
| other non-2xx (`4xx`/`5xx`) | A real failure — includes the mounts `500` defect | Raises `BMDRestError` |
| connection refused / timeout | Transport failure | Raises `BMDConnectionError` |

`get`/`put`/`post`/`delete` all funnel through one `_request()`. A `session` can be
injected (real or fake) for testing — `tests/unit/rest/test_client.py` never opens a
real socket. Every log line is prefixed `[<host>]`, the REST sibling of CLAUDE.md's
`[<ble_name> @ <address>]` convention.

**Two URL namespaces on one host.** `/control/api/v1/...` (`API_BASE`) is not the whole
HTTP surface — `/mounts/...` is the Web Media Manager, a separate namespace at the host
root, which this doc's own mounts walk (`walk_mounts()`, above) already builds requests
for without `API_BASE`. `RestClient.get()`/`exists()` take `api_prefixed: bool = True` for
exactly this reason; `RestCameraSession.list_mount()`/`mount_names()`/`path_exists()` pass
`api_prefixed=False`. This was a real defect on first real-hardware use
(`docs/rest/session.md`'s "`list_mount(path)`" section) — `RestClient` originally
prepended `API_BASE` unconditionally, so every `/mounts/...` call 404'd against a path
that was never real.

`RestClient.exists()` has had a churny history worth recording plainly: added for an
original filename-probing photo-confirmation design, removed once a second real-hardware
defect the same day retired that design in favor of a `Stills`-directory-`mtime` signal
that needs no filenames, then reintroduced again once the redesigned mechanism worked on
hardware and a follow-up request asked for the captured still's name — this time as a
deliberately opt-in, informational-only lookup (`rest/media.py`'s
`guess_new_still_path()`) that never gates the actual confirmation. See
`docs/rest/session.md` and `docs/ble/photo_capture.md` §11 for the full sequence.

### `RestEventRouter` (`src/bmd_camera/rest/events.py`)

Deliberately mirrors `NotificationRouter`'s `arm()`/`wait_for()` contract byte for byte
— same staleness/duplicate-delivery discipline (see docs/ble/session_and_verification.md)
— keyed by property path string instead of `(category, parameter)`, and buffering
`propertyValueChanged` values instead of decoded BMD packets.

Message shapes come from the uploaded `Notification.yaml` AsyncAPI spec, not a guess:

```json
{"type": "request",  "id": 0, "data": {"action": "subscribe", "properties": ["/transports/0/record"]}}
{"type": "response", "id": 0, "data": {"success": true}}
{"type": "event",    "data": {"action": "propertyValueChanged",
                               "property": "/transports/0/record", "value": {"recording": true}}}
```

Two things carried over directly from lessons this migration already learned the hard
way:

- **Reconnection resubscribes.** Subscriptions are per-connection — exactly the BLE CCCD
  lesson in docs/ble/winrt_ble_connection_hardening.md. `connect()` remembers every
  property `subscribe()` was called for and resubscribes them automatically after a
  reconnect.
- **A connection-generation guard.** `connect()` bumps a generation counter; the
  background reader task checks it before routing each message, so a reader from a
  connection that's still winding down can never deliver into the router after a newer
  `connect()` call — the same guard class BLE's reconnect loop uses.
- **Response/event confusion is exactly what broke the first sweep run** (see "Known
  gaps in the first run" above — WS responses matched positionally instead of by `id`).
  `RestEventRouter` never faces that problem in the first place: `handle_event` only
  ever routes a well-formed `propertyValueChanged` event (`is_property_event`); a
  `response` message, or any other event type, is silently ignored rather than
  misattributed.

**Confirmed against real hardware, 2026-08-04**: `tools/rest/watch_events.py` run against
`POCKET_6K_PRO v8.6` over USB, subscribed to all 48 `websocket_properties` from its
profile. The event shape above is exactly what arrived — no gap between spec and wire on
this camera/firmware. Three things worth recording from that run:

- `/transports/0/timecode`, `/timelines/0`, `/system/format`, and `/media/workingset` all
  delivered `propertyValueChanged` events with `value` matching their `Notification.yaml`
  schema (or, for `/media/workingset`, the same shape `GET /media/workingset` already
  returns — see "Storage" above).
- **`/system`'s event `value` is `None`**, not a dict — consistent with `GET /system`
  returning `204`/empty on this camera. A caller cannot assume every event's `value` is a
  mapping; `/system` is at minimum one property where it isn't.
- `/media/workingset` is not in `Notification.yaml`'s documented `deviceProperty` enum
  but subscribed and emitted real content anyway — the same "undocumented but real"
  pattern already established for `/camera/id`/`/presets`/`/presets/active` from the
  `/event/list` sweep.

`/transports/0/timecode` fired roughly every 80ms during this run (an idle camera, not
recording) — a caller subscribing to it should expect a high-frequency stream, not an
occasional update.

### Profile plumbing (`payloads/rest_schema.json`, `camera_profile.py`)

A `rest/<firmware>.json` profile is optional per camera — `CameraProfile.for_model()`
loads it if present, exactly matching the sibling `ble/<firmware>.json`'s shape:

```json
{
  "_meta": {"model_key": "POCKET_6K_PRO", "firmware": "v8.6", "status": "UNVERIFIED"},
  "transport": "usb",
  "endpoints": {
    "/system/format": {"status": 200, "supported": true, "put_status": 204, "put_supported": true}
  },
  "websocket_properties": ["/system/format", "..."],
  "format_names": {},
  "provenance": {"status": "CANDIDATE", "method": "...", "verified_on": "2026-08-03", "notes": "..."}
}
```

This is exactly the shape `tools/rest/probe_endpoints.py`'s `build_rest_profile_block()`
emits — the same "paste the tool's output, never hand-author it" discipline
`tools/control/discover_command.py` established for BLE `commands` blocks. Absent
entirely for a camera with no sweep yet: `profile.rest` is then an all-defaults
`RestProfile`, mirroring a Phase 1 BLE scaffold's `commands == {}`.

`profile.rest_endpoint(path)` / `profile.require_rest_endpoint(path)` follow the
existing `command()` / `require_command()` pair exactly. Both real cameras now have a
profile, transcribed from the write-probe sweeps above:

| Profile | Endpoints | WS properties | Source |
|---|---|---|---|
| `payloads/models/POCKET_6K_G2/rest/v8.6.json` | 76 | 46 | The third (write-probe) sweep run, before the `/lens/focus` fix — its `put_status` is absent for `/video/ndFilter/displayMode` (no value to echo back, ND-less camera) and `/lens/focus` (the spec-key bug, fixed afterward but not re-swept) |
| `payloads/models/POCKET_6K_PRO/rest/v8.6.json` | 76 | 48 | The fourth sweep run, after the `/lens/focus` fix — its `/lens/focus` entry carries a real `put_status`/`put_supported` the G2's profile lacks |

Both are `_meta.status: "UNVERIFIED"` and `provenance.status: "CANDIDATE"` — a same-value
`PUT` proves an endpoint exists, not that a changing write applies (Phase 5's job). A
re-sweep of the G2 with the fixed catalog would fill in its `/lens/focus` write result;
nothing currently depends on that gap.

### `tools/rest/watch_events.py`

The WebSocket analogue of `examples/monitor_incoming.py` — connects, subscribes (either
an explicit `--properties` list or a profile's confirmed `websocket_properties`), and
logs every `propertyValueChanged` event as it arrives via `RestEventRouter`'s `on_event`
callback hook. Unlike `probe_endpoints.py` (deliberately standalone, no `bmd_camera`
imports), this tool exercises the Phase 2 library surface directly — it is the first
consumer of `RestEventRouter` outside its own tests. Confirmed against real
`POCKET_6K_PRO v8.6` hardware 2026-08-04 — see "Confirmed against real hardware" above.

### `tools/rest/smoke_test_client.py`

`watch_events.py` only opens the WebSocket and observes — it never calls `RestClient`'s
`get()`/`put()`, and never arms a write. This tool exercises exactly the two pieces that
leaves untested:

- **Read-only (always runs)**: confirms `RestClient`'s full status-code contract against
  real responses — `GET /system` -> `None` (`204`), a confirmed-`200` endpoint -> a parsed
  body, a confirmed-`501` endpoint -> `BMDUnsupportedError`. Endpoints are chosen from the
  target camera's own `rest/<fw>.json` profile, never hardcoded.
- **`--verify-write` (opt-in, typed-yes gated)**: one full round trip through the exact
  primary/secondary dual-check pattern design principle 3 specifies for REST —
  `RestEventRouter.arm()` → idempotent same-value `PUT` via `RestClient` →
  `RestEventRouter.wait_for()` (primary) → a `GET` readback (secondary). The property is
  auto-picked as the first one in the profile that is both `put_supported` and a
  confirmed `websocket_properties` member (so `wait_for()` has something to observe), or
  overridden with `--property`.

This was the first real exercise of `arm()`/`wait_for()` against live hardware — the exact
verification primitive `RestCameraSession.record_start`/`record_stop` (Phase 4) and
`set_camera_format` (Phase 5, docs/rest/session.md) now build on, both real-hardware-
confirmed in their own right.

**Confirmed against real hardware, 2026-08-04** — both modes run against `POCKET_6K_PRO
v8.6` over USB:

- Read-only: all three checks passed exactly as designed. `GET /system` -> `None`.
  Auto-picked `GET /audio/channel/0/available` -> `{'available': True}`. Auto-picked
  `GET /system/codecFormat` -> raised `BMDUnsupportedError`.
- `--verify-write`: auto-picked `/audio/channel/0/input` (`put_supported` and a confirmed
  `websocket_properties` member). `PUT {'input': 'Camera - Left'}` (same-value) — the
  **primary check succeeded**: `wait_for()` returned `{'input': 'Camera - Left'}` within
  the WS event stream, not a timeout falling back to the secondary. The `GET` readback
  agreed. This is the first end-to-end proof that the exact dual-check pattern design
  principle 3 specifies for REST — event primary, readback secondary — actually closes
  the loop on real hardware, not just against fakes in the test suite.

Between this and `watch_events.py`'s confirmed run above, every piece of Phase 2's
library surface (`RestClient`'s three status branches, `RestEventRouter`'s event parsing,
and now `arm()`/`wait_for()` itself) has real-hardware evidence behind it.

### `tools/rest/sweep_camera_format.py`

The REST analogue of `tools/control/sweep_camera_format.py` (Phase 5, docs/rest/session.md
has the full write-up) — runs `RestCameraSession.set_camera_format()` across every
`(codec, variant, resolution, fps)` combination a profile's tables claim, expanded by one
live `GET /system/supportedFormats` read into one sweep item per distinct
`sensorResolution` the camera pairs with it. Built directly from this session's own
`sensorResolution` finding above: two manual `set_camera_format()` calls in a row were
enough to surface a real defect, and nothing in this codebase's tooling checked
systematically for a similar one elsewhere.

**Real hardware, `POCKET_6K_G2 v8.6`, 2026-08-03**, full sweep, no filters: **544
confirmed, 16 unsupported, 0 unconfirmed.** The 16 unsupported are exactly the `BRAW`/`6K`
`59.94`/`60` combinations `GET /system/supportedFormats` doesn't offer — a real hardware
ceiling, correctly pre-filtered without a write attempted, not a bug. Every combination the
camera claims to support, across the full codec/quality/resolution/fps/sensor-area space,
confirmed cleanly.

Same run also caught a genuine defect one level down, in `RestCameraSession` itself, not
this tool: every confirmed write took ~6 seconds (`verify_timeout_s`'s default), a uniform
timing signature across all 544 that turned out to mean the WS-event primary channel never
had a chance to fire — `__aenter__` had never subscribed the router to `/system/format` at
all, so every write fell through to the secondary `GET` readback after burning the full
primary timeout first. Fixed by subscribing to `/system/format` alongside
`/transports/0/record` at connect time — see docs/rest/session.md's "Connection lifecycle"
section.

**Fix confirmed, `POCKET_6K_G2 v8.6`, 2026-08-04**: an identical full re-sweep — same 544
confirmed, 16 unsupported, 0 unconfirmed — with per-combination timing now 0.0–0.2s (a
handful of 1.1s outliers) instead of the uniform ~6.0s the pre-fix run showed.

### `tools/rest/verify_low_storage.py`

Real-hardware verification for `RestCameraSession.wait_for_low_storage()` (Phase 8 item 1,
`docs/rest/session.md`'s `last_known_storage`/`wait_for_low_storage()` section) — built
because that method's threshold-crossing logic, immediate-return shortcut, and
return-value contract had only run against the injected-fake unit test suite. Connects,
prints a baseline `storage_state()` snapshot, calls `wait_for_low_storage()` with the
thresholds given on the command line, and reports the result, elapsed time, and the final
`last_known_storage` snapshot. Records nothing itself — crossing a real threshold means
running a real recording concurrently (`examples/rest_record_test_clip.py` or the camera
body), which is why this tool's own docstring recommends a smaller card (128GB) than the
1TB card every other real-hardware run in this doc used — a meaningful threshold is
otherwise impractical to reach in one session.

**Two real runs, `POCKET_6K_G2 v8.6`, 2026-08-05** (128GB card):

1. `--min-space-bytes 10000000000 --timeout 1800`: a real concurrent recording took the
   card from `117.57 GB` down to `55.02 GB` remaining, tracked correctly and continuously
   throughout, but never reached the 10GB threshold — `False` after the full 1800s,
   correctly. The "storage stays healthy" branch.
2. `--min-space-bytes 50000000000 --timeout 300`, run immediately after (card now at
   `33.96 GB`, already below 50GB): returned `True` after 3.1s — not instant, since
   `last_known_storage` was still `None` at call time (called right after connect), so this
   genuinely waited on the first real `/media/workingset` push to arrive and cross. The
   harder "live crossing mid-wait" branch, now confirmed end to end.

Only the immediate-return shortcut specifically (already-populated, already-low
`last_known_storage` at call time) has no dedicated real-hardware run of its own yet — see
docs/rest/session.md's `last_known_storage`/`wait_for_low_storage()` section for the full
write-up and how low-risk that residual gap is.

---

## Security note

No authentication and no TLS appear anywhere in the specs or in anything observed. This
is plaintext control over a point-to-point USB link, which is a materially lower exposure
than the same API on a shared network — worth remembering if the transport ever moves to
Wi-Fi.
