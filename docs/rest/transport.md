# REST / WebSocket Transport

**Status:** Phase 0 — the endpoint sweep tool (`tools/rest/probe_endpoints.py`) is
implemented; **no sweep has been run yet**, and no REST client, session, or profile
exists. Everything below the "Sweep results" heading is empty on purpose, waiting for
real-hardware evidence.

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
| `GET /mounts/<volume>/Stills/` | `500 Internal Server Error` | **Firmware defect** — the parent and the individual files both work |

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

*(Empty. Populate from a real run — one section per `(model_key, firmware, transport)`,
in the evidentiary style of `docs/ble/settings.md`: what was run, what came back, and
what remains unexplained. Do not fill this in from the specs.)*

### `POCKET_6K_G2 v8.6` over USB

Not yet swept. Confirmed by hand before any tooling existed, by browser. The scheme
recorded at the time was `https://`, but that has not held up: on 2026-08-03 port 443
refused for both the probe tool and `curl`, while Setup → Network Access advertises
`http://` and is set to `Enabled` rather than `Enabled with security only`. Treat the
browser's address bar as unreliable evidence of the scheme — it silently upgrades and
falls back. The endpoints themselves are unaffected:

| Endpoint | Status | Notes |
|---|---|---|
| `GET /system/format` | works | codec, frameRate, offSpeedEnabled, offSpeedFrameRate, recordResolution, sensorResolution |
| `GET /video/iso` | works | `{"iso": 400}` |
| `GET /video/whiteBalance` | works | `{"whiteBalance": 5600}` |
| `GET /video/shutter` | works | `{"shutterAngle": 18000}` |
| `GET /media/workingset` | works | active media, device name, clip count, remaining/total space, remaining record time |
| `GET /clips/list` | works | e.g. `{"clipUniqueId": 3, "filePath": "/mnt/sd0/A001/A001_07311253_C001.mov"}` |
| `GET /mounts/` | works | |
| `GET /mounts/<volume>/` | works | |
| `GET /mounts/<volume>/Stills/<file>.dng` | works | direct download by known filename |
| `GET /mounts/<volume>/Stills/` | **500** | firmware defect; treat as permanently unusable |
| `GET /system` | **204** | no body |
| `GET /system/videoFormat` | **501** | |
| `GET /system/codecFormat` | **501** | |
| WS subscribe `/media/active` | works | `{"type": "response", "success": true}` |
| WS subscribe `/system/format` | works | |
| WS subscribe `/transports/0/record` | works | |

**No write endpoint has been exercised on any camera.**

Note the WS response shape above: `success` arrives at the **top level**, while
`Notification.yaml` documents it nested under `data`. The tool accepts both.

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
