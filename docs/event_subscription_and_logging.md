# Event Subscription and Logging

## Overview

`BMDCameraController.connect()` automatically subscribes to three BLE characteristics —
INCOMING_CONTROL, TIMECODE, and CAMERA_STATUS — via `subscribe_all()`. Every notification
is logged as uppercase hex pairs and written to a per-session file under `logs/`.

---

## Subscription Strategy

### Three characteristics, always subscribed

| Characteristic | Constant | Direction | Default handler | Log prefix |
|---|---|---|---|---|
| INCOMING_CONTROL | `CHARACTERISTIC_INCOMING` | Indicate | `_log_incoming` | `RX:` |
| TIMECODE | `CHARACTERISTIC_TIMECODE` | Notify | `_log_timecode` | `TIMECODE:` |
| CAMERA_STATUS | `CHARACTERISTIC_CAM_STATUS` | Read / Notify / Write | `_log_camera_status` | `CAM_STATUS:` |

### `subscribe_all()`

The single entry point for establishing all three subscriptions. Called from `connect()`
after the BLE link is confirmed up. Passes the stored callback for each characteristic:

- **First call** (just after connect): all three stored callbacks are `None` → default
  hex-logging handlers are used.
- **After reconnect**: stored callbacks hold whatever handler was active before the drop
  (default or user-supplied) → the same handler is restored automatically.

```python
async def subscribe_all(self) -> None:
    await self.subscribe_incoming(callback=self._incoming_callback)
    await self.subscribe_timecode(callback=self._timecode_callback)
    await self.subscribe_camera_status(callback=self._camera_status_callback)
```

### Per-characteristic subscribe methods

`subscribe_incoming`, `subscribe_timecode`, and `subscribe_camera_status` share identical
structure:

1. **Pre-flight guard** — raises `RuntimeError` if `_client` is `None` or `is_connected`
   is `False`.
2. **Handler resolution** — `callback if callback is not None else self._log_<name>`.
3. **Generation-guarding wrapper** — wraps the resolved handler before passing it to
   `start_notify` (see below).
4. **Retry loop** — up to 3 attempts with a 10 s delay between them. Retries on
   `BleakError` or `OSError`. Fast-fails immediately on a `"not connected"` `BleakError`
   (connection lost mid-subscribe; retrying would not help).
5. **Callback storage** — stores the **raw** resolved handler (not the wrapper) in
   `_<name>_callback` for reconnect restoration.

### Generation-guarding wrapper

Every `start_notify` call receives a `guarded_handler` closure, not the raw handler
directly:

```python
def guarded_handler(char, data):
    self._last_rx_time = time.monotonic()   # always — proves BLE transport is live
    if self._conn_gen != my_gen:            # stale session
        logger.debug("Dropping stale notification …")
        return
    handler(char, data)                     # live session — dispatch
```

`my_gen` is captured at subscribe time and matches the `_conn_gen` value of the connection
that registered this handler. When `connect()` is called again (reconnect), `_conn_gen` is
incremented, so every wrapper from the previous session silently drops its notifications
without touching the new session's state.

The `_last_rx_time` update happens **before** the generation check. Even a stale
notification from a superseded session proves the BLE transport is live — this is the
authoritative liveness signal on WinRT where `BleakClient.is_connected` is unreliable.
See `docs/winrt_ble_connection_hardening.md` for details.

### Reconnect restoration

`_reconnect_loop` calls `connect()` after a successful explicit reconnect attempt.
`connect()` calls `subscribe_all()`, which passes the stored `_<name>_callback` values.
Because those are the raw handlers (set during the previous successful subscribe), all
three characteristics are re-subscribed with the correct handlers — no explicit
re-subscription logic is needed in `_reconnect_loop`.

### Custom callbacks

Callers can replace the default logging handler for any characteristic by calling the
individual subscribe method directly after `connect()`:

```python
await cam.connect()  # auto-subscribes all three with defaults

def my_incoming_handler(char, data):
    ...

await cam.subscribe_incoming(callback=my_incoming_handler)
# now _incoming_callback = my_incoming_handler
# this handler is restored automatically on the next reconnect
```

---

## File Logging Strategy

### Per-instance child logger

```python
self._logger = logging.getLogger(f"bmd_ble.camera_controller.{model_key}")
```

Created in `BMDCameraController.__init__`. All log calls inside the class use
`self._logger` (not the module-level `logger`). The child logger propagates to the root
logger, so console output configured by the caller via `logging.basicConfig` is unchanged.
The file handler is additive.

### Log file path

```
logs/<model_key>_<firmware>/<model_key>_<firmware>_<timestamp>.log
```

- Relative to the script's CWD.
- Directory is created with `mkdir(parents=True, exist_ok=True)` in `__init__`.
- Timestamp format: `%Y%m%dT%H%M%S` (e.g. `20260619T143022`).
- One file per `BMDCameraController` instance (i.e. per script run). Reconnects
  **append** to the same file — they do not create a new one.

Example: `logs/POCKET_6K_G2_v7.9/POCKET_6K_G2_v7.9_20260619T143022.log`

### Format

```
%(asctime)s.%(msecs)03d  %(levelname)-8s  %(name)s  %(message)s
```

Date format: `%Y-%m-%dT%H:%M:%S`. Milliseconds appended after the dot. Level
left-padded to 8 chars. Logger name is the child logger name (`bmd_ble.camera_controller.POCKET_6K_G2`).

### Camera identity prefix

Every log line that involves a BLE operation includes `[BLE_NAME @ ADDRESS]`:

```
2026-06-19T14:30:22.015  DEBUG     bmd_ble.camera_controller.POCKET_6K_G2  [A:AF3DC814 @ AA:BB:CC:DD:EE:01] RX: 00 06 0A 01 00 01
2026-06-19T14:30:22.016  INFO      bmd_ble.camera_controller.POCKET_6K_G2  [A:AF3DC814 @ AA:BB:CC:DD:EE:01] Subscribed to TIMECODE
```

This allows log lines from multiple controller instances to be distinguished when they
share a console stream.

### Log levels in practice

| Level | Examples |
|---|---|
| `DEBUG` | RX/TIMECODE/CAM_STATUS hex bytes; stale notification drops; gen-guard mismatches |
| `INFO` | connect / disconnect; "Subscribed to …"; metadata reads |
| `WARNING` | Reconnect scheduled; subscribe retry; unreliable GAP / Device Info reads |
| `ERROR` | Reconnect attempt failed |
| `CRITICAL` | All reconnect attempts exhausted — camera offline |
