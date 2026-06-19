# bmd-ble-framework

Python package (`bmd_ble`) for automated Blackmagic Design camera control over Bluetooth Low Energy.

**Platform:** Windows only (WinRT BLE stack)  
**Python:** 3.11 / 3.12  
**Status:** In active development — profiles are UNVERIFIED until tested on real hardware

---

## Supported cameras

| Model key | Camera | Firmware | Status |
|---|---|---|---|
| `POCKET_6K_G2` | Pocket Cinema Camera 6K G2 | v7.9 | In progress — primary reference |
| `POCKET_6K_PRO` | Pocket Cinema Camera 6K Pro | v8.6 | In progress |
| `URSA_BROADCAST_G2` | URSA Broadcast G2 | v7.5 | Planned |
| `URSA_MINI_PRO_12K` | URSA Mini Pro 12K | v8.1 | Planned |
| `POCKET_4K` | Pocket Cinema Camera 4K | v8.6 | Planned |

---

## Target operations

- Record start / stop
- Settings changes: codec, quality, resolution, FPS
- Photo capture
- Video playback / gallery browsing
- Video and photo metadata capture
- Storage media monitoring (SD card state, remaining space, slot status)

---

## Installation

### Clone

```bash
git clone https://github.com/D-B-Dilip-Kumar-Reddy/bm-ble-framework.git
cd bm-ble-framework
```

### Install runtime dependencies

```bash
pip install -r requirements.txt
```

### Install development dependencies

```bash
pip install -r requirements-dev.txt
```

---

## Quick start

### Scan for a camera

```bash
python examples/scan_camera.py
```

### Connect and inspect

```bash
python examples/connect_to_camera.py
```

### Monitor raw INCOMING_CONTROL notifications

```bash
python examples/monitor_incoming.py
```

Edit `MODEL_KEY` / `FIRMWARE` at the top of each script to target your camera.
Set `MONITOR_DURATION_S` in `monitor_incoming.py` to a positive integer to auto-stop
after that many seconds; leave it as `None` to run until Ctrl+C.

---

## Package structure

```
src/bmd_ble/
  __init__.py               # Public API — CameraProfile, constants, KNOWN_PROFILES
  constants.py              # BLE UUIDs and timing constants (fixed by spec)
  exceptions.py             # BMDConnectionError, BMDTimeoutError, BMDCommandError,
                            # BMDVerificationError, BMDUnsupportedError, BMDStorageError
  scanner.py                # BLE discovery by advertisement name
  camera_profile.py         # Load, validate, and cache model/firmware profiles
  camera_controller.py      # BLE transport layer — connect, disconnect, notify, reconnect
  protocol/                 # BMD packet encoding/decoding (in progress)

payloads/
  models/                   # One JSON file per (MODEL_KEY, firmware) pair
  schema.json               # JSON Schema — validated at profile load time

examples/
  scan_camera.py            # Discover cameras by BLE advertisement name
  connect_to_camera.py      # Connect and read device identity metadata
  monitor_incoming.py       # Stream raw INCOMING_CONTROL notifications

tests/
  unit/                     # No hardware required — 84 tests, runs in CI
```

---

## Connection management

`BMDCameraController` handles all BLE transport concerns:

- **Auto-reconnect** — on unexpected disconnect, retries up to `RECONNECT_MAX_ATTEMPTS`
  times with increasing delays. Aborts early if the camera auto-reconnects at OS level
  (detected via `is_connected` or active notification flow).
- **WinRT liveness signal** — `BleakClient.is_connected` is unreliable on Windows WinRT.
  The controller uses incoming notification timestamps (`_last_rx_time`) as the
  authoritative connection-health signal. If notifications are flowing, the link is alive.
- **Connection generation guard** — each `connect()` call increments an internal
  generation counter. Notification handlers and disconnect callbacks from superseded
  sessions are silently discarded, preventing duplicate streams after power-cycles.
- **Serialised connect** — an `asyncio.Lock` ensures only one `BleakClient` is created
  at a time, even if the user script and the reconnect loop race.
- **Clean shutdown** — `disconnect()` is idempotent and stops all CCCD subscriptions
  before closing the link. Example scripts wrap sessions in `try/finally` to guarantee
  disconnect on Ctrl+C.

---

## Profile JSON

Every camera/firmware combination has a profile in `payloads/models/`:

```
POCKET_6K_G2_v7.9.json
POCKET_6K_PRO_v8.6.json
```

All protocol values (codec IDs, quality variants, FPS encodings, category/parameter
pairs) live in the profile — never in Python code. A profile must be `"VERIFIED"` before
production use; unverified profiles log a prominent warning at session start.

---

## Development

### Run unit tests

```bash
pytest tests/unit/ -v
```

All 84 unit tests run without hardware. CI runs on Windows, Python 3.11 and 3.12.

### Adding a new camera

1. Run `tools/sniffers/` while performing target actions on the camera
2. Extract category, parameter, data type, and payload bytes from captures
3. Create `payloads/models/<MODEL_KEY>_<FIRMWARE>.json`
4. Add the tuple to `KNOWN_PROFILES` in `camera_profile.py`
5. Run `pytest tests/unit` — no Python code changes needed for new profiles
6. Test on real hardware and set `"status": "VERIFIED"`

### Adding a new command

1. Capture via sniffer on the target camera/firmware
2. Add protocol values to the profile JSON
3. Implement encoder in `protocol/categories/<category>.py`
4. Expose through `session.py`
5. Write unit test with mocked BLE client

---

## Known limitations

- `_is_receiving_data()` requires the camera to be sending notifications. A connected but
  idle camera (no characteristic changes) will appear disconnected after the 3-second
  liveness threshold and trigger an unnecessary reconnect attempt. No data is lost.
- GAP identity reads (`read_gap_identity_metadata`) are disabled for most profiles —
  the 6K G2 disconnects if GAP reads are attempted at the wrong time.
- Protocol categories are not yet populated — sniffer sessions are needed to fill
  the command tables in `CLAUDE.md` and the profile JSONs.

---

## Licence

See `LICENSE`.
