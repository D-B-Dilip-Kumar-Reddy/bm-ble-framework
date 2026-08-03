"""
.claude/hooks/doc_map.py
========================
Shared source-path -> required-doc mapping used by both doc-sync hooks
(require_docs_on_commit.py, remind_docs_on_edit.py).

The mapping mirrors CLAUDE.md's "Supplementary Documentation" table and
package structure exactly. When a new module or docs/<feature>.md is added,
add its glob here in the same commit — this file is the single place the
mapping lives, per CLAUDE.md's own "Feature doc convention".

Stdlib only: hooks run outside any guarantee that the project venv is
active, so no third-party imports are allowed here.
"""

from __future__ import annotations

from fnmatch import fnmatch

CLAUDE_MD = "CLAUDE.md"

# Directories whose changes require a doc touch (CLAUDE.md's own scope for
# the doc convention). tests/ and examples/ are deliberately excluded.
SCOPED_DIR_PREFIXES = ("src/", "tools/", "protocol/", "payloads/")

# (glob pattern, [required doc paths]) — first matching pattern wins.
# Patterns are matched against a path relative to the repo root.
DOC_MAP: list[tuple[str, list[str]]] = [
    (
        "src/bmd_camera/ble/protocol/codec.py",
        ["docs/ble/packet_structure_and_constants.md", "docs/ble/protocol.md"],
    ),
    (
        "src/bmd_camera/ble/protocol/types.py",
        ["docs/ble/packet_structure_and_constants.md", "docs/ble/protocol.md"],
    ),
    ("src/bmd_camera/ble/protocol/categories/recording.py", ["docs/ble/recording.md"]),
    ("src/bmd_camera/ble/protocol/categories/storage.py", ["docs/ble/recording.md"]),
    ("src/bmd_camera/ble/protocol/categories/settings.py", ["docs/ble/settings.md"]),
    (
        "src/bmd_camera/ble/camera_controller.py",
        [
            "docs/ble/winrt_ble_connection_hardening.md",
            "docs/ble/event_subscription_and_logging.md",
        ],
    ),
    ("src/bmd_camera/ble/notification_router.py", ["docs/ble/session_and_verification.md"]),
    ("src/bmd_camera/ble/session.py", ["docs/ble/session_and_verification.md"]),
    ("src/bmd_camera/ble/timecode.py", ["docs/ble/timecode.md"]),
    ("src/bmd_camera/ble/scanner.py", ["docs/ble/protocol.md"]),
    (
        "src/bmd_camera/ble/constants.py",
        ["docs/ble/protocol.md", "docs/ble/packet_structure_and_constants.md"],
    ),
    ("src/bmd_camera/ble/state.py", ["docs/ble/session_and_verification.md"]),
    ("src/bmd_camera/camera_profile.py", ["docs/ble/payload_profiles.md"]),
    ("src/bmd_camera/exceptions.py", [CLAUDE_MD]),
    ("tools/common/capture.py", ["docs/ble/sniffer_capture_engine.md"]),
    ("tools/common/discovery.py", ["docs/ble/command_discovery.md"]),
    ("tools/sniffers/*", ["docs/ble/sniffer_capture_engine.md"]),
    (
        "tools/control/*",
        ["docs/ble/active_camera_control.md", "docs/ble/command_discovery.md"],
    ),
    ("tools/rest/*", ["docs/rest/transport.md"]),
    ("payloads/ble_schema.json", ["docs/ble/payload_profiles.md"]),
    ("payloads/rest_schema.json", ["docs/rest/transport.md"]),
    ("payloads/models/*/rest/*", ["docs/rest/transport.md"]),
    ("payloads/models/*/ble/*", ["docs/ble/payload_profiles.md"]),
]

# Anything under a scoped directory that matches nothing above still needs
# *some* doc touch — fall back to requiring CLAUDE.md rather than silently
# skipping the check.
FALLBACK_REQUIRED_DOCS = [CLAUDE_MD]


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def is_scoped(path: str) -> bool:
    """Whether a changed path falls under the doc-sync enforcement scope."""
    path = _normalize(path)
    return path.startswith(SCOPED_DIR_PREFIXES)


def _docs_for_path(path: str) -> list[str]:
    path = _normalize(path)
    for pattern, docs in DOC_MAP:
        if fnmatch(path, pattern):
            return docs
    return FALLBACK_REQUIRED_DOCS


def required_docs(changed_paths: list[str]) -> dict[str, list[str]]:
    """Map each doc required by the given changed paths to the source
    files that triggered the requirement.

    Only paths within ``SCOPED_DIR_PREFIXES`` contribute requirements;
    out-of-scope paths (tests/, examples/, CI config, docs/ itself) are
    ignored, since a doc-only or test-only change never gates on itself.
    """
    result: dict[str, list[str]] = {}
    for path in changed_paths:
        if not is_scoped(path):
            continue
        for doc in _docs_for_path(path):
            result.setdefault(doc, []).append(_normalize(path))
    return result


def unsatisfied_docs(changed_paths: list[str]) -> dict[str, list[str]]:
    """Like ``required_docs``, but drops any doc requirement already
    satisfied by ``changed_paths`` itself (the mapped doc, or CLAUDE.md,
    was also part of the same change set)."""
    changed = {_normalize(p) for p in changed_paths}
    reqs = required_docs(changed_paths)
    if CLAUDE_MD in changed:
        return {}
    return {doc: sources for doc, sources in reqs.items() if doc not in changed}
