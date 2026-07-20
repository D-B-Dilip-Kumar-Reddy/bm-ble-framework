# bmd-ble-framework

Python package (`bmd_ble`) for automated Blackmagic Design camera control over
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

- Record start / stop *(implemented — echo-verified on real hardware)*
- Settings changes: codec, quality, resolution, FPS *(implemented — CANDIDATE values from an external reverse-engineering doc, pending re-verification on real hardware; see [`docs/settings.md`](docs/settings.md))*
- Photo capture *(planned)*
- Video playback / gallery browsing *(planned)*
- Video and photo metadata capture *(planned)*
- Storage media monitoring — SD card state, remaining space, slot status *(planned)*

---

## Installation

```bash
git clone https://github.com/D-B-Dilip-Kumar-Reddy/bm-ble-framework.git
cd bm-ble-framework
pip install -r requirements.txt        # runtime
pip install -r requirements-dev.txt    # development (tests, lint)
```

## Quick start

User scripts import from `bmd_ble` — the public API is `CameraSession`
(async context manager) plus `CameraProfile`/`get_profile`/`KNOWN_PROFILES`
and `BMDVerificationError`. Example scripts:

```bash
python examples/scan_camera.py          # discover cameras by BLE advertisement name
python examples/connect_to_camera.py    # connect-only smoke test (connect, hold, disconnect)
python examples/monitor_incoming.py     # stream raw INCOMING_CONTROL notifications
python examples/record_start_stop.py    # echo-verified record start/stop via CameraSession
```

Edit `MODEL_KEY` / `FIRMWARE` at the top of each script to target your
camera. `record_start_stop.py` **starts a real recording** on the connected
camera — use deliberately.

---

## Documentation map

| Document | Covers |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Project reference: architecture, package structure, design principles, camera registry, workflows for adding cameras/commands, testing and logging conventions |
| [`docs/protocol.md`](docs/protocol.md) | Full protocol reference — SDI categories/parameters, data types, operations, BLE GATT layer, spec-vs-sniffer divergences |
| [`docs/packet_structure_and_constants.md`](docs/packet_structure_and_constants.md) | Packet header layout and the `protocol/codec.py` design |
| [`docs/payload_profiles.md`](docs/payload_profiles.md) | Per-camera profile JSONs, schema validation, provenance |
| [`docs/session_and_verification.md`](docs/session_and_verification.md) | `CameraSession`, echo-based write verification |
| [`docs/recording.md`](docs/recording.md) | The record start/stop command family |
| [`docs/settings.md`](docs/settings.md) | Codec/quality/resolution/FPS families, the BRAW↔ProRes switch, and their verification runbook |
| [`docs/winrt_ble_connection_hardening.md`](docs/winrt_ble_connection_hardening.md) | Connection management on Windows/WinRT: reconnect loop, liveness detection, known limitations |
| [`docs/event_subscription_and_logging.md`](docs/event_subscription_and_logging.md) | Notification subscriptions and per-session file logging |
| [`docs/sniffer_capture_engine.md`](docs/sniffer_capture_engine.md) | Reusable BLE capture engine behind the reverse-engineering tools |
| [`docs/active_camera_control.md`](docs/active_camera_control.md) | Active send-and-capture tooling (`tools/control/`) |
| [`docs/command_discovery.md`](docs/command_discovery.md) | Guided discovery of new commands on real hardware |

## Development

```bash
python -m pytest tests/unit/                                # unit tests (no hardware)
python -m ruff check . && python -m ruff format --check .   # lint + format
```

CI runs the unit suite on Windows, Python 3.11 and 3.12. See `CLAUDE.md` for
the full testing policy and the workflows for adding a new camera or command.

## Licence

See `LICENSE`.
