# Reverse-Engineering Workflow

Extracted from CLAUDE.md (moved verbatim, not rewritten — CLAUDE.md's own rule for a
primary-reference move, applied here too: rewriting an evidentiary write-up "would
fabricate provenance"). This is the concrete, tool-by-tool procedure for bringing up a
new `(MODEL_KEY, FIRMWARE)` pair, and the checklist for adding a single new command to
an already-brought-up camera. CLAUDE.md keeps a short pointer to this doc.

---

## Workflow: Adding Support for a New Camera (Reverse-Engineering Procedure)

This is the concrete, tool-by-tool procedure for bringing up a new `(MODEL_KEY, FIRMWARE)`
pair — which tool to run in which order, and what profile change each step produces.
Follow the phases in order; each one depends on the profile state the previous phase left
behind. Derived from three bring-ups at different stages:

| Bring-up | Stage reached | Evidentiary write-up |
|---|---|---|
| `POCKET_6K_G2 v7.9` | All phases (frozen — hardware upgraded away, see the registry) | `docs/ble/recording.md`, `docs/ble/settings.md` |
| `POCKET_6K_PRO v8.6` | Phase 2 done; Phase 3 in progress — resolutions, dimension_enums, and codec ids transcribed, nothing yet promoted past CANDIDATE | `docs/ble/settings.md` §15–§17 |
| `POCKET_6K_G2 v8.6` | **Phase 3 complete for all three settings families** (2026-07-30) — the current primary reference, and the live worked example of the firmware-upgrade variant below. Steps 9-13 all done: passive sweep, fps sweep, full `dimension_enum` sweep (all 8 confirmed), nine manual confirming writes, and a 432/480 `sweep_camera_format.py` round trip promoted `codec_quality`/`video_format`/`recording_format` to VERIFIED. Two systematic gaps found (ProRes/4K DCI retarget; BRAW/6K/high-fps) mirror PRO precedents but aren't yet promoted to guarded fields — evidence bar not yet met on this firmware. Phase 4 (whole-profile `_meta.status` promotion) still open: `capabilities`/`storage` remain unpopulated | `docs/ble/recording.md` ("Per-camera status"), `docs/ble/settings.md` §18, §18.7, §18.9, §18.10 |

`docs/ble/command_discovery.md` covers Phase 2's tooling; `docs/ble/settings.md` covers Phase 3's.

### Which camera a script talks to

Three conventions to keep straight — the defaults all point at the primary reference
(`POCKET_6K_G2 v8.6`), so **any of these run with no flags will target it**:

| Script group | How the camera is selected |
|---|---|
| `tools/query/*`, `tools/sniffers/*`, `tools/control/send_record_command.py` | `--model-key`/`--firmware` flags, **defaulted** via module-level `DEFAULT_MODEL_KEY`/`DEFAULT_FIRMWARE` constants |
| `tools/control/discover_command.py`, `send_settings_command.py`, `sweep_dimension_enum.py`, `sweep_camera_format.py` | `--model-key`/`--firmware` flags, **`required=True` — no default**. Deliberate: these change real camera state or emit profile blocks, so the target is never implicit |
| `examples/*.py` | No CLI flags at all — `MODEL_KEY`/`FIRMWARE` are module-level constants near the top of the file. Edit them, or uncomment the alternate model's line where one is already present |

When bringing up a *non-primary* camera, pass `--model-key`/`--firmware` explicitly on
every command in the phases below (as they are written) rather than relying on the
default — a defaulted run against the wrong camera is silent, not an error.

### Two variants of this procedure

**A brand-new model** (e.g. `URSA_BROADCAST_G2 v7.5`) starts from nothing: every phase
runs in full, with no prior expectations about any value.

**A new firmware for a model already reverse-engineered** (e.g. `POCKET_6K_G2`
v7.9 → v8.6, the 2026-07-27 upgrade) is the same procedure with one difference in
posture, not in steps. Design principle 6 still forbids inheriting any protocol value
from the old profile — but the old profile is the best available *hypothesis source*,
so seed each phase's candidates from it and let the camera confirm or refute them.
Phase 2 step 8.2's `--values 2,0 --reserved 1,0` is exactly this pattern in practice.
Two concrete rules for this variant:

- `_meta.ble_name` **does** carry over when it is the same physical unit — the
  advertisement name identifies the hardware, not the firmware. Re-confirm it with
  `examples/scan_camera.py` (Phase 1 step 2) anyway; that step is cheap and the whole
  bring-up depends on it.
- Nothing else carries over. Every codec id, dimension_enum, category/parameter pair,
  and fps encoding must be re-sniffed on the new firmware even when you fully expect
  it to be unchanged, and each lands in the new profile with its own `provenance`
  naming its own capture.

### Phase 1 — Profile scaffold and transport sanity

1. **Scaffold the profile.** Add `payloads/models/<MODEL_KEY>/ble/<FIRMWARE>.json` with only
   `_meta` (`model`, `model_key`, `firmware`, `ble_name` — the real advertised name, never
   a placeholder — `status: "UNVERIFIED"`) and `ble` populated. No `commands` yet.
   Validate against `payloads/ble_schema.json` — only `_meta` is schema-required, so a
   two-section scaffold is a valid profile and loads cleanly (`commands` is simply `{}`).
   `payloads/models/POCKET_6K_G2/ble/v8.6.json` is the reference for what this stage looks like.

   **Register it in `KNOWN_PROFILES` now, not at Phase 4**, if any Python default is
   going to point at this camera (see step 14 — the rule is only ever "never before the
   JSON exists"; the scaffold *is* the JSON). Two consequences to expect immediately:
   - Every `KNOWN_PROFILES`-parametrized unit test starts running against the scaffold.
     Tests asserting sniffed content must skip a profile that has none rather than
     demand it — `test_every_known_profile_resolves_write_margin_warning_storage_signal`
     skips when `profile.storage` is empty, and is the pattern to copy.
   - Loading it logs the design-principle-8 `status is UNVERIFIED` warning on every run.
     That is working as intended for the whole bring-up; it clears at Phase 4.
2. **Confirm discoverability** — `python examples/scan_camera.py` (after setting
   `MODEL_KEY`/`FIRMWARE`). Confirms the camera actually advertises under `ble_name`.
3. **Confirm a bare connect works** — `python examples/connect_to_camera.py`. Connect,
   hold, disconnect — nothing else.
4. **Confirm GATT UUIDs match expectations** —
   `python tools/query/ble_services_chars.py --model-key <MODEL_KEY> --firmware <FIRMWARE>`.
   If a characteristic UUID differs from `constants.py`'s default, override it under the
   profile's `ble` section (e.g. `characteristic_incoming`) — never edit `constants.py`
   for a per-model value (design principle 1).
5. **Check GAP metadata readability** —
   `python tools/query/gap_meta_data.py --model-key <MODEL_KEY> --firmware <FIRMWARE>`.
   Record the result in `gap_meta_data.readable`. Known hazard: on `POCKET_6K_G2 v7.9`,
   reading GAP characteristics disconnects the camera — if the new model does the same,
   set `readable: false` and don't retry the read anywhere else for this camera.
   **That hazard is firmware-specific, not model-specific** — re-checked on
   `POCKET_6K_G2 v8.6` (2026-07-28) on the same physical unit and it did not reproduce:
   both GAP characteristics read back cleanly with no disconnect, so that profile
   records `readable: true` where v7.9 records `false`. A worked example of the
   firmware-upgrade rule above: the old profile's value was a hypothesis, and the
   camera refuted it.
6. **Check device-info metadata readability** —
   `python tools/query/device_meta_data.py --model-key <MODEL_KEY> --firmware <FIRMWARE>`.
   Record the result in `device_info_meta_data.readable`.
7. **Confirm notifications stream** — `python examples/monitor_incoming.py` (after
   setting `MODEL_KEY`/`FIRMWARE`). Watch raw INCOMING_CONTROL bytes while performing a
   few obvious actions on the camera body — gives a first feel for the protocol before
   any targeted sniffing begins.

### Phase 2 — Recording start/stop

8. Sniff → discover → paste → verify → session round-trip:
   1. `python tools/sniffers/sniffer_recording.py --model-key <MODEL_KEY> --firmware <FIRMWARE>`
      — passive capture while the operator starts/stops recording on the camera body.
   2. `python tools/control/discover_command.py --model-key <MODEL_KEY> --firmware <FIRMWARE> --label recording --from-capture <path to the capture saved by step 8.1> --values 2,0 --reserved 1,0 --outcomes start,stop`
      — seeds candidates from the passive capture, sweeps them with operator confirmation
      per candidate, emits the ready-to-paste `commands.recording` block (see
      `docs/ble/command_discovery.md`). `--reserved 1,0` tries the G2's known reserved byte
      first; a genuinely new family may need different candidates.

      Two failure modes seen for real on the `POCKET_6K_G2 v8.6` run (2026-07-29):
      - **The right triple can be filtered out of the seeded list.** `--from-capture`
        drops triples present in *every* window as ambient telemetry, but a start/stop
        pair reports the same `(category, parameter)` in *both* windows — so the
        recording family itself was excluded and all three offered candidates were
        dead ends. **Fixed 2026-07-29**: a triple whose payload is stable within each
        window but differs between them is now recognised as a state report and kept
        (`docs/ble/command_discovery.md`). If a family still goes missing from the seeded
        list, seed it manually with `--category`/`--parameter`/`--data-type` — the
        capture shows you the coordinates.
      - **A reserved-byte-indifferent family can't be emitted.** If the camera acts
        on more than one reserved byte, two candidates confirm the same outcome and
        no single block can describe them — a `commands` entry carries one scalar
        `reserved`. The tool prints a summary of every confirmation with its echo
        state and exits 1 (it does not emit, and does not traceback). Resolve it by
        hand: prefer the reserved value with a clean wire echo for every outcome, and
        record the indifference in `provenance.notes`.
   3. Paste the emitted block into the profile's `commands.recording`, then
      `pytest tests/unit` — no Python code should need to change; the protocol layer
      already handles the recording category generically.
   4. `python tools/control/send_record_command.py --model-key <MODEL_KEY> --firmware <FIRMWARE>`
      — deterministic active send-then-capture, confirming the pasted block's echo on
      demand rather than waiting to catch it inside a passive window.
   5. `python examples/record_start_stop.py` (after setting `MODEL_KEY`/`FIRMWARE`) — the
      real `CameraSession.record_start()`/`record_stop()` round trip with echo
      verification. Passing this promotes `commands.recording.provenance.status` to
      `"VERIFIED"`.

### Phase 3 — Settings: codec, quality, resolution, FPS

9. **Sniff all three families passively**, one capture window per concrete setting so
   each result is unambiguously attributable:
   ```
   python tools/sniffers/sniffer_settings.py --model-key <MODEL_KEY> --firmware <FIRMWARE> \
       --actions res_HD,res_UHD,res_4K_DCI,codec_prores,codec_braw,quality_variant_change,fps_change
   ```
   (adjust the action list to the model's actual resolutions/codecs). The operator
   performs each change on the camera body between prompts. This is expected to confirm
   `codec_quality` (`0x0A/0x00`) and `recording_format` (`0x01/0x09`) reports — on the G2,
   `video_format`'s own channel (`0x01/0x00`) never reported passively at all, so its
   `dimension_enum` values could not be captured this way; assume the same here unless
   proven otherwise, and probe them actively next.
10. **Probe every (resolution, codec) `dimension_enum` actively**, one candidate at a
    time:
    ```
    python tools/control/send_settings_command.py --model-key <MODEL_KEY> --firmware <FIRMWARE> \
        --packet video_format --fps <fps> --dimension-enum 0x<candidate>
    ```
    Typed-yes gated; the operator watches the body, and the resulting `0x01/0x09` report's
    width/height plus the on-screen codec identifies what the enum selects. Repeat per
    candidate until every needed (resolution, codec) pair has a confirmed enum, or the
    search is exhausted for a pair like the G2's 4K DCI/ProRes (see `docs/ble/settings.md`
    §7-§9 for that precedent and the two-step `set_camera_format` proxy workaround it
    needs when a gap can't be closed). For an **exhaustive** sweep across many untried
    candidates in one connected session — e.g. hunting a still-missing enum like
    ProRes/4K DCI — use `tools/control/sweep_dimension_enum.py` instead of repeating
    single-candidate sends by hand: it decodes each result from the wire automatically
    (`--target-resolution`/`--target-codec`) rather than requiring the operator to read
    the on-screen display, which is known-unreliable on at least one camera (see below).
    See `docs/ble/active_camera_control.md` for the full writeup.
    **The G2's proxy workaround is not guaranteed to generalize:**
    on `POCKET_6K_PRO v8.6`, the equivalent proxy workaround's second step
    (`set_recording_format` retargeting resolution within the proxied-to codec) never
    confirms for ProRes/4K DCI, confirmed 2/2 via real `CameraSession` round trips —
    not a G2 pattern to assume elsewhere (see `docs/ble/settings.md` §16). A passive
    capture of the camera reaching the same state through its own body menu then
    confirmed the target state itself is real and correctly held/reported by the
    camera (§16 addendum) — so this is a gap in the write path this codebase uses,
    not proof the camera-side combination is unsupported. Don't assume a retarget
    failure like this means "unreachable" without checking whether the camera can
    be observed in that state at all.
11. **Transcribe confirmed values into the profile** — `commands.codec_quality` /
    `commands.video_format` / `commands.recording_format`, plus the `codecs` /
    `resolutions` / `fps_modes` lookup tables. Nothing may be copied from another model's
    profile (design principle 6); every value must come from this model's own capture.
12. **Confirm each family's write+echo cycle**, one deterministic active send per family:
    ```
    python tools/control/send_settings_command.py --model-key <MODEL_KEY> --firmware <FIRMWARE> \
        --packet codec_quality --codec <codec> --variant <variant>
    python tools/control/send_settings_command.py --model-key <MODEL_KEY> --firmware <FIRMWARE> \
        --packet video_format --resolution <resolution> --codec <codec> --fps <fps>
    python tools/control/send_settings_command.py --model-key <MODEL_KEY> --firmware <FIRMWARE> \
        --packet recording_format --resolution <resolution> --fps <fps>
    ```
    Add `--repeat 2` to any of these to also probe that family's redundant-write echo
    behavior (send the identical command twice; compare whether the second window shows
    `(none observed)`) before relying on a `last_known_*` no-op guard for it in
    `session.py` — every family went silent on a repeated identical write on the G2
    (`docs/ble/settings.md` §11, §14), but that must be reconfirmed per model, not assumed.
13. **Session round-trip** — `python examples/change_codec.py` (after setting
    `MODEL_KEY`/`FIRMWARE`). Runs `CameraSession.set_camera_format()`, which orchestrates
    all three writes with echo verification. Passing this promotes all three families'
    `provenance.status` to `"VERIFIED"`.

    This confirms one combination. Before trusting the profile's full `codecs`/
    `resolutions`/`fps_modes` tables, run `tools/control/sweep_camera_format.py
    --model-key <MODEL_KEY> --firmware <FIRMWARE> --dry-run` first to see the full
    combination count, then a real (possibly narrowed) sweep — it runs
    `set_camera_format()` across every combination the tables claim is supported and
    flags any that never confirm, the same shape of gap `POCKET_6K_PRO v8.6`'s
    ProRes/4K DCI combination turned out to be (`docs/ble/settings.md` §16) after being
    found by accident rather than checked systematically. An `unconfirmed` result is a
    candidate for a `known_unreachable` entry, not an automatic one — it still needs
    the same real-hardware follow-up that investigation took (see
    `docs/ble/active_camera_control.md`) before being written into the profile.

### Phase 4 — Finish

14. Add the `(MODEL_KEY, FIRMWARE)` tuple to `KNOWN_PROFILES` in `camera_profile.py`, if
    Phase 1 step 1 didn't already. The rule is "never before the profile JSON exists"
    (see "What Not To Do") — not "never before Phase 4".
15. **Promote `_meta.status` to `"VERIFIED"`** now that every populated section has been
    confirmed on real hardware. This is the whole-profile status; each command block's
    own `provenance.status` was promoted as its phase completed.
16. Run `pytest tests/unit` and `ruff check . && ruff format --check .` — both must pass
    before committing.
17. Update the camera registry table at the top of this file (status column, notes).

#### If this camera becomes the primary reference

Making a camera the primary reference is a separate, deliberate change — the registry
table's "Primary reference" designation and every Python default must move together, or
a defaulted run silently targets the old camera. The `POCKET_6K_G2` v7.9 → v8.6 move
(2026-07-28) is the worked example:

1. Repoint every default named in "Which camera a script talks to" above —
   `DEFAULT_MODEL_KEY`/`DEFAULT_FIRMWARE` in `tools/query/`, `tools/sniffers/`, and
   `tools/control/send_record_command.py`; `MODEL_KEY`/`FIRMWARE` in every
   `examples/*.py`. Grep for the outgoing firmware string to confirm none were missed.
2. Repoint the `MODEL_KEY`/`FIRMWARE` identity constants in the **mocked** unit tests
   (`test_session.py`, `test_camera_controller.py`) — they are labels on
   `_from_raw`/`SimpleNamespace` profiles, so this is free. Tests that call
   `CameraProfile.for_model(...)` against a **real** JSON for its populated
   `codecs`/`resolutions`/`fps_modes` tables must stay on the old profile until the new
   one is populated (`test_send_settings_command.py`, `test_sweep_camera_format.py`,
   `test_sweep_dimension_enum.py` are still on v7.9 for exactly this reason).
3. Update the registry table: the new primary gets the **Primary reference** note, and
   the outgoing one is marked `Frozen` with why it can no longer be tested and why it is
   nonetheless kept (evidentiary record, and tests that still load it).
4. Leave the `docs/` write-ups alone. They are the historical evidence for the camera
   and firmware they were gathered on; a primary-reference move does not invalidate a
   single finding in them, and rewriting them to the new firmware would fabricate
   provenance.

Every profile JSON change in this procedure needs a doc touch in the same commit, per the
"Feature doc convention": `docs/ble/recording.md` for Phase 2, `docs/ble/settings.md` for Phase 3.

### Phases not yet defined

Photo capture, playback, and metadata have **no phase here** because no camera has
completed them. The photo trigger itself is confirmed on two cameras, but with no
BLE-observable confirmation signal it cannot satisfy design principle 3, so there is no
repeatable procedure to write down yet — see `docs/ble/photo_capture.md` §7/§9 for the open
verification-strategy question that blocks it. `POCKET_6K_G2 v8.6`'s USB/HTTP media
access is the most promising route to an out-of-band confirmation channel.

---

## Workflow: Adding a New Command

1. Capture the command via sniffer on the target camera/firmware
2. Add protocol values (category, param, data type, payload encodings) to the profile JSON
3. Add encoder function in `protocol/categories/<category>.py`
4. Define the expected echo or `CAMERA_STATUS` mask for verification
5. Expose the command through `session.py`
6. Write unit test with a mocked BLE client — covers encode, send, echo, verify
7. Test on real hardware before marking profile `VERIFIED`

