# WinRT BLE Connection Hardening

**Status:** implemented — all mechanisms below are live in `camera_controller.py`.

## Overview

The BLE connection management layer (`BMDCameraController`) is hardened against
the specific quirks of the Windows WinRT BLE stack: an unreliable
`BleakClient.is_connected`, OS-level auto-reconnects that bypass the
application, duplicate disconnect callbacks, and CCCD subscriptions that
survive a power-cycle. This doc describes the resulting design — the
connection-generation guard, the connect-lock, the RX-timestamp liveness
signal, and the reconnect loop — and the eight observed failure modes each
piece exists to fix.

*History:* this hardening was done on branch
`claude/bm-ble-architecture-review-2shv8h`, triggered by hardware tests with
`monitor_incoming.py` on a Pocket Cinema Camera 6K G2 (v7.9): power-cycling
the camera produced false "Camera offline" critical logs, duplicate
notification streams, and a camera screen that stayed "Connected" after the
script exited. All eight root causes below were traced and fixed.

---

## Connection-management internals in `camera_controller.py`

### `BMDCameraController.__init__`

Internal fields that drive the hardening:

| Field | Type | Purpose |
|---|---|---|
| `_conn_gen` | `int` | Connection generation counter — incremented on every `connect()` call |
| `_connect_lock` | `asyncio.Lock` | Serialises concurrent `connect()` calls |
| `_reconnecting` | `bool` | Guards against duplicate `_reconnect_loop` tasks |
| `_incoming_callback` | `Callable \| None` | Stores the raw user callback for re-subscription after explicit reconnect |
| `_last_rx_time` | `float \| None` | Monotonic timestamp of the most recent INCOMING_CONTROL notification |

### `connect()`

- Acquires `_connect_lock` for the duration of the call — prevents two concurrent
  BleakClient objects from being created (e.g. user script + reconnect loop racing).
- **Idempotency guard**: if `_client.is_connected` is already True when the lock is
  acquired, returns immediately without creating a new client.
- Increments `_conn_gen` and captures it in the `on_disconnect` closure. Stale closures
  from superseded connections check their captured generation and return early.
- Resets `_intentional_disconnect = False` at entry so that calling `connect()` after an
  intentional disconnect/reconnect cycle re-enables the reconnect loop for future drops.
- Nulls `_client` if `BleakClient.connect()` raises, preventing a partial reference
  from being left behind.

### `connect()` retry loop and `_connect_and_subscribe_once()`

`connect()` resolves the address, then calls `_connect_and_subscribe_once()` up to
`CONNECT_SUBSCRIBE_MAX_ATTEMPTS` (3) times, sleeping `CONNECT_RETRY_DELAY_S` (2 s)
and calling `_discard_client()` between attempts. It retries **only** when the
failure is a mid-subscribe link drop (`_is_mid_subscribe_drop`, matching the
markers `_subscribe_retry_blocked_by` emits); every other failure — a connect
timeout, an unreachable characteristic, subscribe exhaustion on a live link —
propagates on the first attempt, because rebuilding the session would only hide a
real fault behind more attempts.

Why this exists: on `POCKET_6K_G2 v8.6` (2026-07-29) the camera dropped the link
roughly 300 ms after the link came up, *inside* `connect()`'s own `subscribe_all()`,
on 3 of 6 observed connects. It is transient — the identical command succeeded on
the other 3 — and it correlated with how long the connect itself took (every
connect under 2 s survived, every one over 2.2 s dropped) rather than with anything
being sent. Without this loop a one-shot tool like `discover_command.py` dies before
it can send anything, which is exactly what blocked the v8.6 Phase 2 bring-up.

`_discard_client()` bumps `_conn_gen` **before** dropping the client, so any late
notification or disconnect callback from the abandoned session is a no-op against
the next attempt, then best-effort `disconnect()`s the stale object.

While `_connect_and_subscribe_once` is inside `subscribe_all()` it sets
`_in_connect_subscribe`, and `on_disconnect` checks that flag and skips spawning
`_reconnect_loop`. This is essential: `connect()` holds `_connect_lock` for its
whole body, so a reconnect loop spawned here would block on that lock while
`connect()` is already rebuilding the same session — the two recovery paths would
fight, which is the deadlock shape behind issue 9.

**First real-hardware trigger, 2026-07-31** (`docs/settings.md` §18.12): a
`send_settings_command.py` run against `POCKET_6K_G2 v8.6` hit exactly the
scenario this loop was built for — `connect attempt 1/3 lost the link during
the initial subscribe … retrying in 2.0 s` — followed by a clean reconnect and
a normal run to completion. First observed confirmation that the retry path
works end to end outside the unit tests' mocked scenarios.

### `disconnect()`

Behaviour unchanged. The `monitor_incoming.py` example now wraps every session in
`try/finally` so `disconnect()` is guaranteed to be called on Ctrl+C.

### `subscribe_incoming(callback, retries, retry_delay_s)`

- Wraps the resolved `handler` in a `guarded_handler` closure before passing it to
  `start_notify`. The wrapper:
  - Updates `_last_rx_time = time.monotonic()` **before** the generation check (even
    stale notifications prove the BLE transport is live — critical for WinRT liveness
    detection).
  - Checks `self._conn_gen == my_gen`; drops the notification with a DEBUG log if the
    generation has advanced (stale session data).
  - Calls through to `handler` only if the generation matches.
- Stores the **raw** `handler` (not the wrapper) in `_incoming_callback` so that an
  explicit reconnect creates a fresh wrapper with the new generation.
- Before sleeping between attempts, consults `_subscribe_retry_blocked_by(my_gen)`
  and aborts immediately when the retry provably cannot succeed (see below).
  `subscribe_timecode` and `subscribe_camera_status` carry the identical loop.

### `_subscribe_retry_blocked_by(my_gen)`

```python
def _subscribe_retry_blocked_by(self, my_gen: int) -> str | None
```

Returns the reason a `start_notify` retry cannot succeed, or `None` when the
failure looks transient and the retry is worth waiting for. Two blocking
conditions:

1. `self._conn_gen != my_gen` — a newer `connect()` superseded this session, so
   retrying would attach a handler that `guarded_handler` will drop anyway.
2. `self._client is None or not self._client.is_connected` — the link is gone.

The distinction matters because the retry loop exists for a real firmware
timing gap (the camera not yet accepting CCCD writes), and that case *does*
deserve the sleep. WinRT makes the two hard to tell apart: a link that drops
mid-CCCD surfaces as `OSError` — `[WinError -2147023673] The operation was
canceled by the user` — which is indistinguishable by exception type from a
transient failure. The disconnect callback has already fired by then, so
connection state, not the exception, is what separates them.

### `_is_receiving_data(threshold_s=3.0)` *(new)*

```python
def _is_receiving_data(self, threshold_s: float = 3.0) -> bool
```

Returns `True` if a notification arrived within the last `threshold_s` seconds.
Used by `_reconnect_loop` as the authoritative connection-liveness signal on WinRT,
where `BleakClient.is_connected` is unreliable.

### `_reconnect_loop()`

- Captures `stale = self._client` at entry so the auto-reconnect check always tests
  the correct object even if `self._client` is replaced by a concurrent `connect()`.
- At the **start of every iteration** (pre-delay) and immediately **after every sleep**
  (post-delay), checks both:
  1. `stale.is_connected` — catches OS-level reconnect on non-WinRT stacks
  2. `self._is_receiving_data()` — catches WinRT auto-reconnect where `is_connected`
     lies but notifications are flowing
- Stops stale CCCD subscriptions (via `stop_notify`) exactly once on the stale client,
  just before the first explicit `connect()` attempt.
- Sets and clears `_reconnecting` flag via `try/finally`; `on_disconnect` checks this
  flag to skip duplicate loop spawns.

---

## Issues faced and fixes applied

| # | Observed symptom | Root cause | Fix |
|---|---|---|---|
| 1 | `_reconnect_loop` and `_log_incoming` ran simultaneously after power-cycle | WinRT auto-restores GATT session before the first reconnect delay expires; loop checked `is_connected` only after sleeping | Added pre-delay `is_connected` check at the start of each loop iteration |
| 2 | Duplicate `on_disconnect` fires on some WinRT versions | Bleak fires the disconnected callback twice | `_reconnecting` bool guard in `on_disconnect`; only one loop task is created |
| 3 | Old notification stream continued alongside new session after explicit reconnect | CCCD subscription survives OS-level reconnect; no per-session handler identity | Connection generation counter (`_conn_gen`); `guarded_handler` in `subscribe_incoming` drops data from superseded sessions |
| 4 | `asyncio.Lock` not held during `connect()` — two clients could race | No serialisation around BLE connect | `async with self._connect_lock` wraps the full `connect()` body; idempotency guard inside the lock |
| 5 | `_client` left pointing at a half-open object after a failed `connect()` | `self._client = BleakClient(...)` executed before `await client.connect()` raised | `self._client = None` in the `except` branch of `connect()` |
| 6 | False CRITICAL "Camera offline" even though notifications were flowing (gen 1, current 4) | WinRT `is_connected` returns `False` even when the BLE link is live; all 3 `connect()` attempts failed and incremented `_conn_gen`, making the guarded handler drop every notification | `_last_rx_time` stamped in `guarded_handler` before the gen check; `_is_receiving_data()` checked in the reconnect loop — RX activity overrides `is_connected` as the liveness signal |
| 7 | Camera screen stayed "Connected" after stopping `monitor_incoming.py` | `cam.disconnect()` was a bare call at the bottom of `main()`; Ctrl+C raised `CancelledError` before it was reached | `try/finally` wraps the connect–monitor block; `disconnect()` is now guaranteed on every exit path |
| 8 | Ctrl+C did not stop `monitor_incoming.py` on Windows | `asyncio.Event().wait()` is not reliably interruptible by task cancellation on the WinRT ProactorEventLoop | Replaced with `while True: await asyncio.sleep(0.5)`; `asyncio.sleep` uses Win32 waitable timers which are reliably cancelled |
| 9 | `discover_command.py` died with `start_notify failed … connection lost mid-subscribe` ~10 s after connect, before any sweep command was sent (POCKET_6K_G2 v8.6, 2026-07-29, twice) | Camera dropped the link ~300 ms into `connect()`'s own `subscribe_all()`. WinRT reported it as `OSError`, which the loop treats as transient, so it slept `retry_delay_s` (10 s) **holding `_connect_lock`** — starving the `_reconnect_loop` that `on_disconnect` had already scheduled. The retry then ran against the dead client and raised the misleading hard error | `_subscribe_retry_blocked_by()` checked before every retry sleep; a dropped link or superseded generation aborts at once, releasing `_connect_lock` ~10 s earlier so the reconnect loop can actually run |
| 10 | After fixing 9 the error became immediate and accurate, but `discover_command.py` still died before sending anything — the camera kept dropping the link ~300 ms after connect (3 of 6 observed connects, POCKET_6K_G2 v8.6, 2026-07-29) | Making the failure honest did not make it recoverable. `connect()` had no retry of its own, and the `_reconnect_loop` that `on_disconnect` spawns cannot help a one-shot tool: it is a background task the dying process never awaits, and it would block on `_connect_lock` anyway | `connect()` now retries `_connect_and_subscribe_once()` up to `CONNECT_SUBSCRIBE_MAX_ATTEMPTS`, but only for a mid-subscribe drop; `_in_connect_subscribe` stops `on_disconnect` spawning a competing reconnect loop while it does |

---

## Test coverage

Every mechanism above is covered by unit tests in
`tests/unit/test_camera_controller.py` (no hardware required, mocked
BleakClient), which run in CI on Windows, Python 3.11 and 3.12 — the suite
has grown well beyond this subsystem since, so no fixed test count is
recorded here.

---

## Known limitations

- `_is_receiving_data()` relies on notifications being present. A camera that is
  connected but sending no notifications (idle, no characteristic changes) will
  still trigger reconnect attempts after the `threshold_s` window. This is a
  conservative safe default — idle cameras get reconnected unnecessarily but data
  is never lost.
- GAP identity reads (`read_gap_identity_metadata`) are gated per profile on
  `gap_meta_data.readable`, as are Device Information reads on
  `device_info_meta_data.readable`. The known hazard — the camera disconnecting
  when GAP reads are attempted at the wrong time — was observed on
  **`POCKET_6K_G2` v7.9**, which is why that profile sets both flags `false`.
  It is **firmware-specific, not model-specific**: re-checked on the same
  physical unit after its v8.6 upgrade (2026-07-28), both GAP characteristics
  read back with no disconnect, so `POCKET_6K_G2 v8.6` sets both flags `true`
  — as `POCKET_6K_PRO v8.6` already did. Don't generalize the v7.9 hazard to a
  model; re-check it per firmware.
- Every camera profile remains `"_meta.status": "UNVERIFIED"` as a *whole
  profile*, even though all three now carry a hardware-verified recording
  family (`commands.recording.provenance.status: "VERIFIED"` — the G2 v8.6's
  promoted 2026-07-29). The profile-level flag stays `UNVERIFIED` until every
  populated section is tested, which is the point of keeping the two statuses
  separate: per-command provenance advances as each family is confirmed, while
  `_meta.status` waits for the whole profile (see `docs/payload_profiles.md`).
