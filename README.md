# bmd-camera-control

Python package (`bmd_camera`) for automated Blackmagic Design camera control over
Bluetooth Low Energy: record start/stop, settings changes, photo capture,
playback, metadata, and storage monitoring — driven entirely from Python
scripts.

**Platform:** Windows only (WinRT BLE stack)
**Python:** 3.11 / 3.12
**Status:** In active development — record start/stop is implemented and
hardware-verified on the Pocket 6K G2/Pro; the remaining operations are
planned. Profiles stay `UNVERIFIED` until every populated section is tested
on real hardware.

This README is a high-level overview only. **`CLAUDE.md` is the
authoritative project reference** (architecture, design principles,
workflows), and each subsystem has a detailed doc in [`docs/`](docs/) — see
the documentation map below.

---

## Supported cameras

| Model key | Camera | Firmware | Status |
|---|---|---|---|
| `POCKET_6K_G2` | Pocket Cinema Camera 6K G2 | v7.9 | In progress — primary reference |
| `POCKET_6K_PRO` | Pocket Cinema Camera 6K Pro | v8.6 | In progress |
| `URSA_BROADCAST_G2` | URSA Broadcast G2 | v7.5 | Planned |
| `URSA_MINI_PRO_12K` | URSA Mini Pro 12K | v8.1 | Planned |
| `POCKET_4K` | Pocket Cinema Camera 4K | v8.6 | Planned |

## Target operations

- Record start / stop *(implemented over BLE — echo-verified on real hardware; implemented over REST — dual-check verified in unit tests, real-hardware confirmation of the `PUT` still pending, see [`docs/rest/session.md`](docs/rest/session.md))*
- Settings changes: codec, quality, resolution, FPS *(implemented — CANDIDATE values from an external reverse-engineering doc, pending re-verification on real hardware; see [`docs/ble/settings.md`](docs/ble/settings.md))*
- Photo capture *(planned)*
- Video playback / gallery browsing *(planned)*
- Video and photo metadata capture — *(planned over BLE; implemented read-only over REST via `RestCameraSession.clips()`, see [`docs/rest/session.md`](docs/rest/session.md))*
- Storage media monitoring — SD card state, remaining space, slot status — *(planned over BLE; implemented read-only over REST via `RestCameraSession.storage_state()`, see [`docs/rest/session.md`](docs/rest/session.md))*

---

## Installation

```bash
git clone https://github.com/D-B-Dilip-Kumar-Reddy/bmd-camera-control.git
cd bmd-camera-control
pip install -e .                       # runtime, editable — makes `bmd_camera` importable
pip install -r requirements-dev.txt    # development (tests, lint)
```

`bmd_camera` lives under `src/`, which is not on Python's import path by default —
`pip install -e .` is what makes `python examples/scan_camera.py` or
`python tools/rest/watch_events.py` work from a fresh clone without also setting
`PYTHONPATH`. (Tests don't need this step: `pytest.ini` sets `pythonpath = src` for
`pytest` itself.) `pip install -r requirements.txt` still works as a dependencies-only
install if you don't want the editable package.

## Quick start

User scripts import from `bmd_camera` — the public API is `CameraSession` (BLE, async
context manager) and `RestCameraSession` (REST/WebSocket — read verbs plus
`record_start`/`record_stop`; format writes are still planned, see
docs/rest/session.md), plus `CameraProfile`/`get_profile`/`KNOWN_PROFILES` and
`BMDVerificationError`. Example scripts:

```bash
python examples/scan_camera.py            # discover cameras by BLE advertisement name
python examples/connect_to_camera.py      # connect-only smoke test (connect, hold, disconnect)
python examples/monitor_incoming.py       # stream raw INCOMING_CONTROL notifications
python examples/record_start_stop.py      # echo-verified record start/stop via CameraSession
python examples/rest_read_state.py        # read current format/storage/clips/timecode over REST
python examples/rest_record_start_stop.py # dual-check-verified record start/stop via RestCameraSession
```

Edit `MODEL_KEY` / `FIRMWARE` at the top of each script to target your
camera. `record_start_stop.py` and `rest_record_start_stop.py` **start a real
recording** on the connected camera — use deliberately.

---

## Documentation map

| Document | Covers |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Project reference: architecture, package structure, design principles, camera registry, workflows for adding cameras/commands, testing and logging conventions |
| [`docs/ble/protocol.md`](docs/ble/protocol.md) | Full protocol reference — SDI categories/parameters, data types, operations, BLE GATT layer, spec-vs-sniffer divergences |
| [`docs/ble/packet_structure_and_constants.md`](docs/ble/packet_structure_and_constants.md) | Packet header layout and the `protocol/codec.py` design |
| [`docs/ble/payload_profiles.md`](docs/ble/payload_profiles.md) | Per-camera profile JSONs, schema validation, provenance |
| [`docs/ble/session_and_verification.md`](docs/ble/session_and_verification.md) | `CameraSession`, echo-based write verification |
| [`docs/ble/recording.md`](docs/ble/recording.md) | The record start/stop command family |
| [`docs/ble/settings.md`](docs/ble/settings.md) | Codec/quality/resolution/FPS families, the BRAW↔ProRes switch, and their verification runbook |
| [`docs/ble/winrt_ble_connection_hardening.md`](docs/ble/winrt_ble_connection_hardening.md) | Connection management on Windows/WinRT: reconnect loop, liveness detection, known limitations |
| [`docs/ble/event_subscription_and_logging.md`](docs/ble/event_subscription_and_logging.md) | Notification subscriptions and per-session file logging |
| [`docs/ble/sniffer_capture_engine.md`](docs/ble/sniffer_capture_engine.md) | Reusable BLE capture engine behind the reverse-engineering tools |
| [`docs/ble/active_camera_control.md`](docs/ble/active_camera_control.md) | Active send-and-capture tooling (`tools/control/`) |
| [`docs/ble/command_discovery.md`](docs/ble/command_discovery.md) | Guided discovery of new commands on real hardware |

## Development

```bash
python -m pytest tests/unit/                                # unit tests (no hardware)
python -m ruff check . && python -m ruff format --check .   # lint + format
```

CI runs the unit suite on Windows, Python 3.11 and 3.12. See `CLAUDE.md` for
the full testing policy and the workflows for adding a new camera or command.

## Licence

See `LICENSE`.
