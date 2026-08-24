# BLE Date/Time (Category 7 — Real Time Clock)

**Status: UNCONFIRMED — investigation just opened, no real-hardware capture
taken yet.** Nothing in this document beyond §1 is anything more than [spec]
transcription. Design principle 6 forbids trusting a category/parameter/
payload encoding until a real sniffer capture confirms it on the specific
camera and firmware — that capture is the next concrete step, not yet taken.

## 1. Why this category, and why now

`docs/ble/protocol.md`'s Category 7 table (transcribed from the official
spec, August 2025 edition) lists three parameters this repo has never
touched:

| Param | Name | Type | Meaning |
|---|---|---|---|
| 7.0 | Real Time Clock | int32 ×2 | [0] time (BCD), [1] date (BCD YYYYMMDD) |
| 7.1 | System language | string | ISO-639-1 two-char code |
| 7.2 | Timezone | int32 | minutes offset from UTC |

This session's actual motivation isn't the category in the abstract — it's a
confirmed, real defect one layer up. `rest/media.py`'s
`guess_new_still_path()` reconstructs a just-captured still's filename from
the *operator's PC clock* (`datetime.now()`), and a real run (`POCKET_6K_G2
v8.6`, 2026-08-13, `examples/rest_delete_still.py`) found the camera's own
onboard clock running **~37h21m behind** the PC — every `minute_offsets`
candidate missed, and the function correctly returned `None` rather than
guess wrong (see `rest/media.py`'s module docstring, "CAMERA CLOCK SKEW").
That docstring already names the fix: pass an `around` value "derived from
the camera's own reported time instead of the operator's wall clock." Category
7.0 — Real Time Clock — is that value. Reading it over BLE, once confirmed,
lets a caller build `around` from ground truth instead of an assumption that
silently breaks whenever the camera's clock has drifted — the goal is for
`guess_new_still_path()`'s guess to be right *every* time it's given a
correct `around`, not just when the operator happens to have set the
camera's clock correctly.

Whether Category 7 also turns out to be **writable** (letting this codebase
*set* the camera's clock, closing the skew at the source rather than working
around it every time) is a secondary question the same capture should start
to answer, but the read/reconstruction path above is the concrete, already-
justified reason this category is being taken up now.

## 2. What we don't yet know

- **Whether BLE exposes a "read current value" operation at all.**
  `docs/ble/protocol.md` §4: only three operation types have ever been
  observed on any camera in this repo — `ASSIGN` (0x00, write), `OFFSET`
  (0x01, spec-only, never seen on the wire), and `CAMERA_REPORT` (0x02, the
  camera announcing a value). Every settings family already in this codebase
  (codec/quality, video format, recording format) is read the same indirect
  way: not by asking, but by capturing what the camera spontaneously
  `CAMERA_REPORT`s when the value *changes*. The working assumption here is
  that Category 7 behaves the same way until a capture proves otherwise.
- **Whether the camera reports 7.0 unprompted on connect** (a "state burst"),
  which would be a much more convenient read path than waiting for an
  operator-triggered change. `tools/sniffers/sniffer_datetime.py`'s window
  model (borrowed as-is from `sniffer_settings.py`) isn't well-positioned to
  catch this reliably — a burst between subscribing and the first window
  opening would be missed — so this is a secondary thing to watch for, not
  the primary capture target.
- **The exact BCD packing** — endianness, whether both int32s are always
  sent together as one 8-byte payload or as two separate parameter reports,
  and whether "BCD" here means packed BCD (two digits per byte) the way
  Category 7's spec description implies, matching the pattern already
  confirmed elsewhere in this protocol (e.g. `TIMECODE`'s own BCD encoding,
  `docs/ble/timecode.md`) or something else entirely. Only a real capture
  settles this.
- **Whether this category is per-model/firmware or genuinely fixed** — like
  every other category in this codebase, no value here may be copied from
  the spec table, or from one camera's confirmed capture to another's,
  without re-sniffing (design principle 6).

## 3. The capture procedure

Follows `docs/ble/reverse_engineering.md`'s "Adding a New Command" workflow,
step 1:

```
python tools/sniffers/sniffer_datetime.py
```

(defaults to the primary reference, `POCKET_6K_G2 v8.6` — pass
`--model-key`/`--firmware` for a different camera, per
`docs/ble/reverse_engineering.md`'s "Which camera a script talks to").

Four windows, one operator-triggered SETUP-menu change each:

1. `view_setup_datetime` — open SETUP > Date/Time without changing anything.
   Cheap, low-confidence check for a report-on-view behavior.
2. `change_date` — change the date by one day.
3. `change_time` — change the time by a few minutes.
4. `change_timezone` — change the timezone, if it's a control separate from
   date/time on this camera's menu (7.2 is its own parameter per the spec
   table).

For each window that shows a `(characteristic, category=0x07, parameter)`
triple, note **exactly** what the operator set the value to (the before and
after date/time/timezone shown on the camera's own screen) — the raw hex
payload is meaningless without knowing what real-world value it's supposed to
represent. The saved capture JSON (`tools/captures/<MODEL_KEY>_<FIRMWARE>/`)
has the full byte-for-byte record; share it alongside the operator-confirmed
before/after values so the payload can be decoded against known ground
truth, the same way every other settings family in this codebase was
transcribed.

## 4. Planned shape once confirmed

Not yet built — recorded here so the plan is visible before any code exists,
per this project's practice of documenting design intent honestly rather than
only after the fact (see e.g. `CLAUDE.md`'s `*(planned)*` tags elsewhere).

- `protocol/categories/datetime.py` — decode (and, if §1's writability
  question resolves yes, encode) for Category 7. Whether this earns its own
  module or folds into `settings.py` depends on how much shared machinery it
  ends up needing; no decision has been made.
- `commands.datetime` (or similarly named) block in the profile JSON, once a
  real capture confirms the coordinates and payload shape — never before.
- `CameraSession.get_datetime()` (and `set_datetime()`, if writable) exposed
  through `session.py`, matching every other capability's transport/protocol
  separation (design principle 5).
- A composed script (mirroring `examples/capture_photo.py`'s BLE+REST
  concurrent-session shape) that reads the camera's real clock over BLE and
  passes it as `guess_new_still_path()`'s `around` argument over REST —
  closing the loop §1 opened with.
- Unit tests with a mocked BLE client (design principle 11's async-first
  discipline applies here like everywhere else), and real-hardware
  confirmation before any profile promotes past `UNVERIFIED` for this
  category, per design principle 8.
