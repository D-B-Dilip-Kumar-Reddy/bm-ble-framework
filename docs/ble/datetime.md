# BLE Date/Time (Category 7 — Real Time Clock)

**Status: CLOSED on both transports — investigation concluded 2026-08-24
after 0-for-6 on real BLE hardware and a definitive negative on the REST
spec.** Six real-hardware BLE runs (§4-§6, §8-§9): three passive (committed
changes, a full connect-time state burst covering 7 categories and 16 Lens
parameters) and three active (`ASSIGN` with default coordinates, `ASSIGN`
with an alternate reserved byte, `OFFSET`). None produced any Category 7
signal in either direction — no report, ever, and no write ever visibly
changed the camera. A direct check of all 11 official REST OpenAPI/AsyncAPI
spec files found zero date/time/clock/NTP-related endpoint anywhere either.
Nothing in this document beyond §1 is anything more than [spec] transcription.
Working conclusion: **neither BLE nor REST exposes this camera's date/time
to a controller at all, on this camera/firmware** — see the "Conclusion"
section (after §9) for the full reasoning and what this means for the
original motivation (fixing `guess_new_still_path()`'s clock-skew problem,
closed by a different route — a widened `minute_offsets` default, see
`rest/media.py`).

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

## 7. `tools/control/send_datetime_command.py` — the active write probe

Built after run 3 exhausted every passive avenue. Sends a Category 7
`ASSIGN` write directly — `--parameter timezone` (plain `int32` minutes
offset, the least ambiguous target, recommended first) or `--parameter rtc`
(`int32 x2`, time+date, both BCD per [spec]; date is packed unambiguously as
`YYYYMMDD`, but the [spec] doesn't specify time's exact BCD shape beyond
"BCD" — this tool's own hypothesis is `HHMMSS00`, by analogy with this
codebase's confirmed `TIMECODE` `HH:MM:SS:FF` shape with the frame digits
zeroed; `--raw-elements` bypasses the guess entirely). `--parameter
language` is not implemented — no string-payload encoder exists in this
codebase yet, and the other two targets are lower-ambiguity first
candidates.

**This tool's payload encoding has zero real capture evidence behind it —
unlike every other active-write tool in this codebase**, which builds
CANDIDATE writes from a profile transcribed off an external RE document.
Nothing it sends should be copied into a profile without independent
real-hardware confirmation (its own module docstring says this explicitly).
A generous `--connect-settle-seconds` default (`12.0`, over the ~8.6s burst
duration §6 observed) avoids the write and its capture window landing inside
the connect-time state dump.

Ground truth is the camera's own SETUP > Date/Time screen, watched by the
operator before and after the send — not any BLE echo, matching
`send_settings_command.py`'s established stance for a write with no
reliable confirmation channel. If the screen changes to match what was sent
but `category=0x07` still never appears in the capture (as in every prior
run), that would show this category is write-only with no BLE-observable
echo at all — the same shape as the photo-capture trigger
(`docs/ble/photo_capture.md`): a real, working write with no way to confirm
it over the wire.

**`--reserved`/`--operation` overrides added after run 4's failure** (§8) —
the same two discovery axes `send_settings_command.py` already exposes for
its own CANDIDATE families, generic across both `timezone` and `rtc`.
**`--operation OFFSET` changes `--minutes`/`--raw-elements`'s meaning to a
delta from the camera's current value, not an absolute target** — this tool
has no way to read the camera's current value to compute that delta itself,
so the operator supplies it directly (e.g. `--minutes 15` to nudge
`UTC+05:30` by 15 minutes under `OFFSET`, not `--minutes 345`).

**Status: real-hardware-run, `POCKET_6K_G2 v8.6`, 2026-08-24 — failed on
every coordinate tried: default `--reserved`/`--operation` (§8), the
alternate reserved byte, and `OFFSET` (§9). See §9 for why this now points
toward a permanent BLE limitation rather than a wrong wire coordinate.**

## 8. Run 4 — active write probe: correctly-formed, camera did not visibly change, `POCKET_6K_G2 v8.6`, 2026-08-24

First real-hardware run of the active write probe: `--parameter timezone
--minutes 345` (targeting `UTC+05:45` from the camera's starting `UTC+05:30`
— a deliberately distinguishable target, per the pre-run discussion). TX
confirmed correctly formed against the [spec] table: `FF 08 00 00 07 02 03
00 59 01 00 00` decodes to category `0x07`, parameter `0x02`, data type
`0x03` (`INT32`), operation `0x00` (`ASSIGN`), payload `0x00000159` = `345`
little-endian — exactly what `build_command()` was supposed to produce, and
it was.

**The operator reported the camera's SETUP > Date/Time screen did not
change** — no visible effect from the write at all. The capture shows the
same signature as every prior run: only ambient `0x09`/`0x00` telemetry, no
`0x07` traffic of any kind (echo or otherwise).

This is a stronger result than a passive null: a plausibly-correct write was
actively sent and the camera did not act on it. Two readings, not yet
distinguished:

1. **This category genuinely does not accept BLE writes on this
   camera/firmware either** — consistent with, and a plausible explanation
   for, why it's never been seen reported: if nothing on this camera/
   firmware implements Category 7 over BLE at all (neither direction), no
   report and no accepted write are both exactly what's expected.
2. **One of the untested coordinates is wrong** — the header's `reserved`
   byte (`0x00` here; `docs/ble/settings.md` documents a real precedent on
   this exact camera where the recording family silently required a
   specific reserved byte a report never revealed, since a camera's own
   REPORT need not carry the value a write requires), the category/parameter
   pair itself (Category 7 could be numbered differently on this firmware
   than the spec table — never checked against anything but the spec), or
   the `ASSIGN` vs `OFFSET` operation choice (`docs/ble/protocol.md` §4 —
   `OFFSET` has never been confirmed accepted for *any* family on this
   camera, and a plain `minutes`-offset semantic parameter is exactly the
   shape `OFFSET`'s documented "add to current value" meaning would suit,
   arguably more than `timezone` alone: a **delta** from the camera's
   current `330` rather than an absolute target).

Both are real possibilities; neither is preferred by the evidence so far.
`--reserved`/`--operation` override flags (mirroring
`send_settings_command.py`'s own discovery axes) are the next concrete
tooling step if this investigation continues — not yet built.

## 9. Runs 5-6 — both remaining discovery axes tried, both failed, `POCKET_6K_G2 v8.6`, 2026-08-24

Two more real-hardware runs, both against `--parameter timezone`, closing
out the two discovery axes §8 identified:

- **Run 5**: `--reserved 0x01` (`ASSIGN`, `--minutes 345`). TX: `FF 08 00 01
  07 02 03 00 59 01 00 00` — header byte 3 correctly shows `0x01`. **No
  visible change on the camera's SETUP screen. No Category 7 traffic** —
  same signature as run 4.
- **Run 6**: `--operation OFFSET` (`--minutes 15`, the delta form per this
  flag's documented semantics — see §7). TX: `FF 08 00 00 07 02 03 01 0F 00
  00 00` — header byte 7 correctly shows `0x01` (`OFFSET`). **No visible
  change on the camera's SETUP screen. No Category 7 traffic** — again the
  same signature.

**Both of the two identified discovery axes are now exhausted, in addition
to the default coordinates run 4 already tried.** Across six real-hardware
attempts total (three passive, three active — default `ASSIGN`, `ASSIGN`
with the alternate reserved byte, and `OFFSET`), Category 7 has never once
produced a BLE-observable effect in either direction — no report, ever, and
no write, of any form tried, ever visibly changed the camera. Reading 1 from
§8 (this camera/firmware genuinely does not implement Category 7 over BLE,
in either direction) is now the far better-supported explanation; reading 2
(an untested wire coordinate) has had its two most-likely candidates
directly tested and ruled out. What remains untested is a much larger and
progressively less-principled space — every possible reserved byte value,
an entirely different category/parameter numbering than the spec table
describes, or other operation/data-type combinations with no particular
reason to expect any of them over the two already tried. Continuing further
would mean brute-forcing rather than reasoned discovery, a different kind of
effort than everything tried so far.

## Conclusion — investigation closed, 2026-08-24

**BLE Category 7 (Real Time Clock / language / timezone) does not appear to
be implemented over BLE at all on `POCKET_6K_G2 v8.6`, in either direction.**
Closed after six real-hardware attempts (three passive, three active) found
zero signal on every avenue this investigation could identify — see §9 for
the full reasoning. This is being recorded as the working conclusion, not
pursued further; §10 below stays as a record of what *would* have been built
had a real capture ever confirmed anything, per this project's practice of
documenting design intent honestly rather than deleting a plan once it's no
longer being pursued.

**The original motivation — fixing `guess_new_still_path()`'s camera-clock-
skew problem — is closed too, by a different route.** No BLE-read camera
clock is possible, so the "feed the camera's real clock into `around`"
design in §1/§10 cannot be built. Instead, this investigation surfaced a
different, directly actionable camera fact along the way: the SETUP >
Date/Time screen has no Seconds field, meaning even an operator who *does*
set the camera's clock manually right before shooting faces a bounded, up-
to-a-couple-of-minutes imprecision (manual entry lag plus an unknowable
committed-seconds value). `guess_new_still_path()`'s default `minute_offsets`
was widened from `(0, 1, -1)` to `(0, -1, 1, -2, 2, -3, 3)` to cover exactly
this — see `rest/media.py`'s module docstring ("SETUP SCREEN HAS NO SECONDS
FIELD") and `docs/rest/session.md`'s `guess_new_still_path()` section for
the full write-up. This remains distinct from, and does not attempt to
cover, the separate unbounded case of a camera whose clock was never set at
all (the original ~37h-skew finding) — that case has no default-window fix
and never will.

**The REST side was also checked, and confirmed closed too, 2026-08-24.**
The 8.6 firmware's REST API is documented across 11 official OpenAPI/AsyncAPI
spec files supplied for this project (`EventControl`, `SystemControl`,
`VideoControl`, `TransportControl`, `MediaControl`, `TimelineControl`,
`LensControl`, `AudioControl`, `ColorCorrectionControl`, `PresetControl`,
`Notification`). A direct grep of all 11 for `date`, `clock`, `ntp`,
`dateTime`/`date-time`, `rtc`, `timestamp`, `timezone`, `utcOffset`,
`wallClock`, `localTime`, and `epoch` found **zero matches in any file**.
`SystemControl.yaml`'s complete path list is `/system`, `/system/format`,
`/system/codecFormat`, `/system/videoFormat`, and their three `supported*`
read-only counterparts — nothing date/time-related at all. This isn't a gap
in what `tools/rest/probe_endpoints.py` happened to discover
(`payloads/models/POCKET_6K_G2/rest/v8.6.json`'s own endpoint list already
showed nothing date-related, consistent with this) — it's confirmation the
capability isn't documented anywhere in the official spec, on either
transport. Combined with the camera's own screenshot showing NTP-based
auto-sync as the offered alternative to manual entry (§1), the most likely
explanation is that this camera's date/time is local UI/OS state (synced via
NTP or set through the SETUP menu directly) rather than a value either
control protocol exposes to a companion app or controller at all — a genuine
camera-design choice, not a software gap in this codebase. No further avenue
on either transport is known; this investigation is closed on both fronts.

## 10. Planned shape once confirmed (not pursued — see Conclusion above)

Not yet built, and no longer expected to be — recorded here so the plan
that was considered stays visible, per this project's practice of
documenting design intent honestly rather than only after the fact (see
e.g. `CLAUDE.md`'s `*(planned)*` tags elsewhere).

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
