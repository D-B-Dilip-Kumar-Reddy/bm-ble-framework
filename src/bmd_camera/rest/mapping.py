"""
bmd_camera/rest/mapping.py
=============================
Codec name derivation between the BLE profile's vocabulary and REST's own
spelling. `RestCameraSession` takes profile *names* (the same vocabulary a
BLE `CameraSession` script already uses — `"BRAW"`/`"5:1"`, `"ProRes"`/
`"422"`) so a script's vocabulary is identical on either transport; this
module supplies the translation.

Confirmed REST codec strings live in a profile's `rest/<firmware>.json`
`format_names` table (design principle 1) — this module supplies only the
DERIVATION RULE used to seed a *candidate* before real evidence confirms
it, exactly like `tools/rest/probe_endpoints.py`'s `build_rest_profile_block()`
leaves `format_names` empty for a human to fill in from evidence.

Confirmed on `POCKET_6K_G2 v8.6` (docs/rest/transport.md, "Codec naming"):

    REST BRaw:   Q0, Q1, Q3, Q5, 3_1, 5_1, 8_1, 12_1
    BLE  BRAW:   Q0, Q1, Q3, Q5, 3:1, 5:1, 8:1, 12:1   (':' -> '_' — a real rule)

    REST ProRes: Proxy, LT, Original, HQ
    BLE  ProRes: Proxy, LT, 422,      HQ               (422 -> Original — NOT a rule)

So BRAW's colon-to-underscore substitution is safe to derive; ProRes's "422"/
"Original" mismatch is exactly why `format_names` must be populated from real
evidence rather than derived — `derive_rest_codec_name` is a seed for that
population step, never a substitute for it once real data exists.

Resolution and fps need no lookup here at all: `"4K DCI" -> {width: 4096,
...}` already comes from the profile's existing `resolutions` table
(shared with BLE), and REST's `frameRate` strings (`"23.98"`, `"24"`, ...)
already match this repo's `fps_modes` keys directly — see
`Notification.yaml`'s `FrameRate` enum.
"""

from __future__ import annotations

# BLE profile family key -> REST family spelling. Only the two families
# confirmed on real hardware so far; an unknown family passes through
# unchanged (better a wrong-but-visible candidate than a silent KeyError).
_REST_FAMILY_NAMES = {
    "BRAW": "BRaw",
    "ProRes": "ProRes",
}


def derive_rest_codec_name(family: str, variant: str) -> str:
    """Best-effort candidate REST codec string from a BLE profile's
    `(codec family, variant)` pair — e.g. `("BRAW", "5:1")` -> `"BRaw:5_1"`.

    Only BRAW's colon-to-underscore substitution is a confirmed rule. This
    is a fallback seed, not a source of truth — always prefer a profile's
    `format_names` entry when one exists (see `resolve_rest_codec_name`).
    """
    rest_family = _REST_FAMILY_NAMES.get(family, family)
    rest_variant = variant.replace(":", "_")
    return f"{rest_family}:{rest_variant}"


def resolve_rest_codec_name(
    format_names: dict[str, dict[str, str]], family: str, variant: str
) -> str:
    """The profile's confirmed `format_names[family][variant]` entry if one
    exists, else the derived candidate (`derive_rest_codec_name`) as a
    fallback. `format_names` is `profile.rest.format_names` — passed in
    rather than a `CameraProfile` directly so this stays a pure function."""
    confirmed = format_names.get(family, {}).get(variant)
    if confirmed is not None:
        return confirmed
    return derive_rest_codec_name(family, variant)


def resolve_ble_codec_name(
    format_names: dict[str, dict[str, str]], rest_codec: str
) -> tuple[str, str] | None:
    """The reverse of `resolve_rest_codec_name`: given a REST codec string
    a camera actually reported (e.g. from `Clip.codec`, `GET /clips/list`),
    find the `(family, variant)` BLE profile pair it came from by scanning
    `format_names` for a matching value.

    Deliberately table-lookup only, with **no** derivation fallback the way
    `resolve_rest_codec_name` has one: `derive_rest_codec_name`'s BLE ->
    REST substitution (`":"` -> `"_"`) is not safely invertible in the other
    direction — ProRes's confirmed `"422"` -> `"ProRes:Original"` entry is
    exactly the counterexample (`mapping.py`'s module docstring) that proves
    no fixed rule can be trusted to guess backwards. Returns `None` when
    `rest_codec` isn't in the table at all, rather than a wrong guess — the
    caller (`RestCameraSession.select_clip`) turns that into a loud
    `BMDUnsupportedError` naming the unmapped string, per design principle
    7, instead of silently trying an invented `(family, variant)`."""
    for family, variants in format_names.items():
        for variant, candidate in variants.items():
            if candidate == rest_codec:
                return family, variant
    return None
