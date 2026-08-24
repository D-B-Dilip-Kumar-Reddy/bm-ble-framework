# BLE Date/Time (Category 7 — Real Time Clock)

**Status: UNCONFIRMED.** Three real-hardware runs so far (§4-§6) — committed
date/time/timezone changes (run 2) and a full connect-time state burst (run
3, 48 notifications across 7 categories, 16 Lens parameters alone) — and
none observed any Category 7 activity on `INCOMING_CONTROL` at all. Nothing
in this document beyond §1 is anything more than [spec] transcription. Every
passive avenue tried so far has come back empty; the one thing not yet
tried is an active controller-initiated write (§6), which is the current
next step.

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

## 4. Run 1 — inconclusive by construction, `POCKET_6K_G2 v8.6`, 2026-08-24

First real-hardware run of `tools/sniffers/sniffer_datetime.py`. All four
windows captured cleanly, but the operator deliberately did **not** press the
SETUP menu's "Update" button after adjusting on-screen values — a correct,
cautious call given this is the first real-hardware exercise of a category
this codebase has never touched, but it means no `ASSIGN` was ever actually
sent to the camera in any window. Every window shows only
`(INCOMING_CONTROL, category=0x09, parameter=0x00)` — the already-documented
ambient ~1/s telemetry (`docs/ble/protocol.md`, Category 9, "mostly ambient
~1/s telemetry, meaning unknown") whose payload jitters constantly regardless
of operator action, unrelated to Category 7. **Zero Category 7 activity in
any window.**

This is not evidence against Category 7 reporting — it's the expected result
of nothing having actually changed. A `CAMERA_REPORT` announces a *changed*
value; scrolling the on-screen wheels without confirming never committed
anything for the camera to report. As a secondary data point, the operator's
screenshot at capture time showed the camera's own manually-set clock reading
`2026-08-24 11:19, UTC+05:30` — close to real time, unlike the earlier
`~37h`-skew case (`rest/media.py`'s module docstring) — so this specific unit
is not currently a live skew case, though that's incidental to this
investigation, not something this run set out to test.

**Next step**: repeat with one real committed change (press "Update" after
adjusting a field, e.g. bump the minute by one) so a genuine `ASSIGN`
actually lands on the camera — reversible, non-destructive, only affects the
camera's own clock display and future media timestamps.

## 5. Run 2 — real committed changes, still zero Category 7 signal, `POCKET_6K_G2 v8.6`, 2026-08-24

Second real-hardware run, same four windows, operator confirmed pressing
"Update" after each of `change_date`/`change_time`/`change_timezone` this
time — genuine `ASSIGN`s (from the camera's own menu, not this codebase)
landed on the camera in every one of those three windows. Result: **still
zero Category 7 activity, in any window.** Every window again shows only
category `0x09`/parameter `0x00` — the same already-documented ambient
telemetry from Run 1 — plus one incidental single-byte `CAMERA_STATUS`
notification (`03`, too short to decode as a command packet; likely an
unrelated status-bit flip, not investigated further here).

Unlike Run 1, this is a **real negative data point**, not an inconclusive
one — three genuine committed changes (date, time, and timezone, each
independently) produced no `INCOMING_CONTROL` traffic on category `0x07` at
all. Two readings, not yet distinguished:

1. **The camera never `CAMERA_REPORT`s Category 7 over BLE at all** — plausible
   given the screenshot in this investigation shows the camera also offers
   NTP-based automatic sync (`time.cloudflare.com`) as an alternative to
   manual entry; date/time may simply not be a value BMD's own protocol
   broadcasts to companion apps the way codec/resolution/recording state is,
   since there's comparatively little reason for an external monitor to need
   to know it live.
2. **A capture-timing gap**: `run_capture_windows`' window model only listens
   between the two Enter presses per label — if the camera reports on some
   delay after the `Update` press (e.g. only once the menu itself closes, or
   only via a mechanism outside `INCOMING_CONTROL`/`CAMERA_STATUS`), the
   report could have landed just outside a window's capture range. Not
   directly evidence for this reading — nothing so far suggests any other
   settings family in this codebase reports on a meaningful delay — but not
   ruled out either.

Neither reading has been distinguished yet; both are consistent with what's
been captured so far. Three options were considered for how to proceed: an
active `ASSIGN` write probe with visual confirmation on the camera's own
screen, a passive connect-burst check, or accepting this as a permanent BLE
limitation and closing the investigation. **Chosen: connect-burst check
first, active probe as the fallback if that's also inconclusive** — cheapest
and lowest-risk option, and it directly targets reading 2 above before
committing to an active write built on a payload encoding with zero real
evidence behind it.

`tools/sniffers/sniffer_datetime.py --burst-seconds N` implements this: a new
`run_immediate_burst_capture()` mode in `tools/common/capture.py` that
subscribes and starts listening immediately, with no operator action and no
"get ready" delay — closing the exact timing gap `run_capture_windows` has
(its first window only opens after the operator's first Enter press, well
after subscription). No real-hardware run of this mode yet.

## 6. Run 3 — connect burst captured, still zero Category 7, `POCKET_6K_G2 v8.6`, 2026-08-24

First real-hardware run of `--burst-seconds 10`. This time the theory
paid off in one sense: **a real, substantial connect-time state burst was
captured** — 48 notifications across 7 distinct categories in about 8.6
seconds (`0x35:33.166` to `0x35:41.748`), including a comprehensive 16-entry
sweep of every Category `0x0C` (Lens) parameter (`0x00`-`0x0F` — lens type
string `"Canon EF-S 18-55mm f/3.5-5.6 IS STM"`, aperture, focal length,
distance range, and more), several Category `0x01` (video/recording),
`0x00` (Lens control), `0x03`, `0x04`, and `0x0A` reports, and the
already-known Category `0x09` ambient telemetry tail. This independently
reconfirms `docs/ble/sniffer_capture_engine.md`'s "Connect-burst
contamination of early windows" finding (previously only observed on two
photo-capture runs, 2026-07-27) on a third, unrelated investigation — the
burst is real, general, and not photo-capture-specific.

**Category `0x07` does not appear anywhere in this burst.** Zero entries,
across every one of the 48 notifications. This is the strongest result yet:
unlike runs 1-2 (which only tested reports-on-change), this run directly
captured the camera's own comprehensive self-announced state dump — a burst
thorough enough to include 16 separate Lens parameters — and Category 7 was
still completely absent from it.

This closes reading 2 from §5 (the capture-timing-gap explanation): the
connect burst was captured in full this time, ruling out "the report existed
but was captured outside any window." Reading 1 now stands essentially
alone: **this camera/firmware does not appear to report Category 7 over BLE
under any circumstance tried so far** (neither on a committed change, nor as
part of its own connect-time full-state announcement). Not yet proven
absolutely — an active write is the one avenue this hasn't tested, since
everything so far has been passive observation of camera-initiated reports
only, never a controller-initiated write.

**Next step, per the plan recorded in §5: the active `ASSIGN` write probe.**

## 7. Planned shape once confirmed

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
