# Photo Capture

**Status:** reverse engineering started — passive sniffer scaffold only
(`tools/sniffers/sniffer_photo.py`). No capture has been run on real hardware
yet. There is no `commands.photo` block in any profile, no
`protocol/categories/media.py`, no `CameraSession` photo API, and no
`examples/capture_photo.py` — all of those remain planned (CLAUDE.md package
structure). Nothing in this doc is hardware-verified.

Target camera for first bring-up: `POCKET_6K_G2 v7.9`, per CLAUDE.md's
camera registry ("start all new features with `POCKET_6K_G2 v7.9`").

---

## 1. Spec starting point

From the official SDI category tables (`docs/protocol.md` §5, all **[spec]**
— starting points for a sniffer session, never values to copy into a
profile):

| Coordinate | Name | Type | Meaning |
|---|---|---|---|
| 10.3 | Still Capture | void | capture a photo |

Category 10 (Media) is the same category as this repo's sniffer-verified
recording command (10.1) and codec_quality (10.0), so the *category* byte
`0x0A` appearing on the wire is well precedented on the G2. Parameter `0x03`
has never been observed in any capture so far — neither as a report nor as
ambient telemetry.

Two spec facts to keep in mind while reading a capture:

- **Void trigger.** The spec types 10.3 as void — no payload. The G2 has
  already shown the spec's type column can diverge from the wire (recording's
  "boolean" parameter takes payload `2` — `docs/protocol.md` §6), so the
  actual write shape is an open question until sniffed/probed.
- **No inverse action.** Unlike record start/stop or a codec round trip,
  a still capture has no paired opposite action. This shapes the sniffer's
  window design (below) and means echo-based verification, if any exists,
  has no state to cross-check against yet.

---

## 2. The passive sniffer: `tools/sniffers/sniffer_photo.py`

Third consumer of the capture engine (`tools/common/capture.py`,
`docs/sniffer_capture_engine.md`) — same connect → `run_capture_windows` →
`print_window_summary` → `save_capture` sequence as the recording and
settings sniffers, with `--actions`-overridable labels like the settings
sniffer.

### Default windows and their rationale

| Window | Operator does | Why it exists |
|---|---|---|
| `idle_baseline` | Nothing | Captures the ambient telemetry floor (categories `0x09`/`0x0C` tick ~1/s on the G2). Because photo capture has no paired opposite action, this window supplies the contrast `seed_triples_from_capture(exclude_ambient=True)` needs — with only photo windows, every window would contain the ambient triples and the filter would keep everything (`docs/command_discovery.md`). |
| `photo_capture_1..3` | One photo each | Three separate single-photo windows make each capture unambiguously attributable and show whether a signal fires on *every* capture (genuine per-photo signal) or only the first (a one-time dump, like the connect-burst reports seen during settings work — `docs/settings.md`). |

`idle_baseline` runs first so a slow after-effect of a capture (e.g. a
delayed storage update) cannot leak into the baseline.

### What to look for in the output

- A triple present in the photo windows but absent from `idle_baseline` —
  the photo-capture report candidate. `0x0A/0x03` would match the spec map,
  but take whatever the wire actually says.
- Category `0x09` movement: a photo consumes card space, so watch whether
  the 9.2 remaining-recording-time hypothesis signal (`docs/protocol.md` §5)
  or anything else in category 9 ticks per photo. That would be the first
  concrete lead toward the remaining-photo-capacity state CLAUDE.md's
  storage gating (design principle 10) will eventually need.
- `CAMERA_STATUS` notifications during the photo windows — recording has no
  known status bit, but photo hasn't been checked at all.

### Known passive limit (precedent)

Some channels never report passively: `video_format` (`0x01/0x00`) never
appeared in any G2 notification across all settings captures
(`docs/settings.md` §5). If every photo window dedupes to only the ambient
triples the idle window also shows, that is a *finding, not a failure* —
record it here, and move to active probing (§3).

---

## 3. Planned path after a first capture (none of this exists yet)

1. **If a passive report exists:** seed
   `tools/control/discover_command.py --from-capture <saved capture>` with
   it, sweep with operator confirmation, paste the emitted `commands.photo`
   block into the profile (`docs/command_discovery.md` — its `--label`/
   `--outcomes` are already command-agnostic; still-capture was an
   anticipated use).
2. **If the trigger is genuinely void (no payload):** there is a real,
   known tooling gap — `encode_assign` deliberately rejects `DataType.VOID`
   (`DATA_TYPE_STRUCT_FORMATS` omits code 0 — `protocol/types.py`), and
   `discovery.py`'s `CandidateCommand.encode()` builds on `encode_assign`,
   so neither the discovery sweep nor any current active tool can send a
   payloadless packet. A void-capable encode path (codec + discovery +
   send-tool support) must be built before active probing of a void 10.3.
   **Do not build it speculatively** — wait for the capture to show whether
   the write is actually void (see the recording precedent in §1: the spec's
   "boolean" carried payload `2` on the wire).
3. **Verification question (open):** what confirms a photo was taken?
   Candidates, all unverified: an echo on the trigger's own coordinates
   (recording's pattern), a storage-state change (§2's category-9 lead), or
   nothing observable at all. Per design principle 3, the photo API cannot
   ship without an answer; per principle 10, storage preconditions
   (card ready, remaining photos > 0) gate the command — both need state
   sources that are themselves still undiscovered.
4. **Profile shape:** one `commands.photo` block, same shape as `recording`
   (`docs/payload_profiles.md` — a trigger family would carry e.g.
   `values: {"trigger": ...}` or, if truly void, no `values` at all; the
   schema already treats `values` as optional).

---

## 4. Open questions

- Does a body-triggered still produce *any* INCOMING_CONTROL report?
- Is the write really void, or does it carry a payload like recording does?
- What storage/state signal, if any, moves per photo?
- Does photo capture require a particular camera state (e.g. not
  recording), and what does the camera report if it's refused (card full,
  no card)?
- Does the still button behave identically across codecs (a BRAW vs ProRes
  attribution session via `--actions` would answer this)?
