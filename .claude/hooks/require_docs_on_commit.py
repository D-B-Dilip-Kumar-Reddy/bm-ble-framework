#!/usr/bin/env python3
"""
.claude/hooks/require_docs_on_commit.py
========================================
PreToolUse hook (matcher: Bash, if: Bash(git commit*)).

Blocks `git commit` when staged changes touch source under CLAUDE.md's
doc-sync scope (src/, tools/, protocol/, payloads/) without also staging
the doc(s) CLAUDE.md maps them to (or CLAUDE.md itself).

Mechanical check only: it guarantees "a doc was touched", not that the
content is correct or that every interconnected doc was chased down — see
doc_map.py's module docstring and CLAUDE.md's "Feature doc convention".
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from doc_map import unsatisfied_docs  # noqa: E402

GIT_COMMIT_RE = re.compile(r"\bgit\s+commit\b")

INTERCONNECT_NOTE = (
    "Review docs/ for interconnected ripple effects beyond the doc(s) listed "
    "above, and update CLAUDE.md too if this touches a design principle, "
    "package structure entry, or convention it documents."
)


def _allow(context: str | None = None) -> None:
    output: dict = {}
    if context:
        output["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    print(json.dumps(output))
    sys.exit(0)


def _deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def main() -> None:
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "")
    cwd = payload.get("cwd") or "."

    if not GIT_COMMIT_RE.search(command):
        _allow()
        return

    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        # Can't determine staged files (not a git repo, git missing, etc.) —
        # fail open rather than block an unrelated command.
        _allow()
        return

    staged = [line for line in result.stdout.splitlines() if line.strip()]
    missing = unsatisfied_docs(staged)

    if not missing:
        _allow(context=f"Doc-sync check passed for this commit. {INTERCONNECT_NOTE}")
        return

    lines = ["Commit blocked: source changes are missing their required doc update.", ""]
    for doc, sources in sorted(missing.items()):
        sources_str = ", ".join(sorted(sources))
        lines.append(f"- {doc} (required by: {sources_str})")
    lines.append("")
    lines.append(
        "Stage an update to the doc(s) above (or to CLAUDE.md, which always "
        "satisfies this check) in the same commit."
    )
    lines.append(INTERCONNECT_NOTE)
    _deny("\n".join(lines))


if __name__ == "__main__":
    main()
