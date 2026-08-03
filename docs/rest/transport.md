# REST / WebSocket Transport

**Status:** Phase 0 — two sweep runs on `POCKET_6K_G2 v8.6` over USB (2026-08-03). The
second run confirms the tool bugs the first run exposed are fixed, and reaches the
mounts hierarchy the first run could not: 53 endpoints working, 5 not implemented, 2
`5xx` (every subdirectory under a mount root, not Stills specifically). Full results
under "Sweep results". No write probe has been run, and no REST client, session, or
profile exists yet.

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
hours]` — **the opposite order** (`docs/ble/timecode.md`). The `Timecode` dataclass and
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

| Question | Answer |
|---|---|
| `GET /system/supportedFormats` works? | **Yes** — full capability matrix, above |
| Plaintext HTTP listener live? | **Yes** — port 80, `Server: BlackmagicDesign` |
| `/clips/list` exposes stills or sizes? | **No size field on clips either** — but the mount root listing does (`size: 49058872` on the one `.mov` present), so a clip's own directory entry can confirm it, just not via `/clips/list` |
| Timecode encoding? | **BCD, big-endian HH:MM:SS:FF**, reversed vs BLE |
| Are the 5xx defects wider than Stills? | **Yes — every subdirectory under a mount root 500s**, not Stills specifically |
| Which WS properties subscribe? | **46/49** — every property except the three `501` `/system/*` endpoints, which the event feed also rejects as unknown |
| `PUT /system/format` implemented? | **Open** — needs `--probe-writes` |
| `PUT /transports/0/record` implemented? | **Open** — never probed, and never will be by this tool (`NEVER_WRITE`) |
| `sensorResolution` writable? | **Open** — but confirmed readable and enumerable, which BLE never achieved |
| USB link survives recording, and BLE concurrently? | **Open** |

### `POCKET_6K_PRO v8.6`

Not swept. Nothing above transfers.

---

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

## New capability REST brings

Beyond replacing BLE functionality, the specs describe control surfaces this repo has
never had: ISO, gain, white balance and tint, shutter, ND filter, auto-exposure; lens
iris, zoom, focus and autofocus; per-channel audio input, level, phantom power, padding,
low-cut filter; the full DaVinci-style colour corrector; and playback with a real
timeline. All of it unverified until swept.

---

## Security note

No authentication and no TLS appear anywhere in the specs or in anything observed. This
is plaintext control over a point-to-point USB link, which is a materially lower exposure
than the same API on a shared network — worth remembering if the transport ever moves to
Wi-Fi.
