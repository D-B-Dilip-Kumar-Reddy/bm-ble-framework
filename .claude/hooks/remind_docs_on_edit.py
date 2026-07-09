#!/usr/bin/env python3
"""
.claude/hooks/remind_docs_on_edit.py
=====================================
PostToolUse hook (matcher: Edit|Write).

Non-blocking reminder: when an edit touches a file under CLAUDE.md's
doc-sync scope (src/, tools/, protocol/, payloads/), inject a note naming
the doc(s) CLAUDE.md maps that file to. Deduped per (session_id, file_path)
so a long back-and-forth on the same file doesn't repeat the nudge.

This is a soft prompt, not enforcement — the hard gate is
require_docs_on_commit.py. This hook exists to surface the requirement
early, before commit time.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from doc_map import is_scoped, required_docs  # noqa: E402

INTERCONNECT_NOTE = (
    "Check whether other docs are affected too, given how interconnected "
    "these subsystems are, and whether CLAUDE.md needs updating."
)


def _already_reminded(session_id: str, file_path: str) -> bool:
    marker_dir = Path(tempfile.gettempdir()) / "bmd_ble_doc_reminders"
    marker_dir.mkdir(exist_ok=True)
    key = hashlib.sha256(f"{session_id}:{file_path}".encode()).hexdigest()
    marker = marker_dir / key
    if marker.exists():
        return True
    marker.touch()
    return False


def _noop() -> None:
    print(json.dumps({}))
    sys.exit(0)


def main() -> None:
    payload = json.load(sys.stdin)
    file_path = payload.get("tool_input", {}).get("file_path")
    session_id = payload.get("session_id", "")

    if not file_path or not is_scoped(file_path):
        _noop()
        return

    if _already_reminded(session_id, file_path):
        _noop()
        return

    docs = required_docs([file_path])
    if not docs:
        _noop()
        return

    doc_list = ", ".join(sorted(docs))
    context = (
        f"You edited `{file_path}` — CLAUDE.md maps this to {doc_list}. "
        f"Update it in this change if behaviour or structure changed. "
        f"{INTERCONNECT_NOTE}"
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context,
                }
            }
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
