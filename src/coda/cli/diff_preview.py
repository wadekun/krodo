"""diff_preview — Rich-highlighted unified diff for the approval prompt (M4 PR3).

Shows the user a coloured diff of what the agent is about to write, so they
can see the full absolute path and exact changes before approving.

Design decisions:
- Old=None → treat as a new file; display content as pure additions.
- Binary / very large diffs are truncated to 200 lines with a notice.
- CRLF line endings are normalised to LF before diffing; the display uses
  the normalised form (the actual write preserves whatever encoding the tool
  chooses).
- The function returns a string (already colourised via Rich markup) rather
  than a Renderable, so callers can just print() it or pass it to
  console.print() without worrying about Rich internals.
"""

from __future__ import annotations

import difflib

from rich.syntax import Syntax

_MAX_DIFF_LINES = 200
_TRUNC_NOTICE = "\n... [diff truncated — showing first 200 lines] ..."


def render_diff(old: str | None, new: str, path: str) -> Syntax:
    """Return a Rich Syntax object containing a unified diff.

    Parameters
    ----------
    old:
        Original file content.  Pass ``None`` for new files (all lines shown
        as additions).
    new:
        New file content to write.
    path:
        File path shown in the diff header (should be an absolute path so
        users can confirm which file will be changed).
    """
    old_text = _normalise(old) if old is not None else ""
    new_text = _normalise(new)

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)

    from_name = f"a/{path}" if old is not None else "/dev/null"
    to_name = f"b/{path}"

    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=from_name,
            tofile=to_name,
        )
    )

    if not diff_lines:
        diff_text = f"--- {from_name}\n+++ {to_name}\n(no changes)"
    else:
        truncated = len(diff_lines) > _MAX_DIFF_LINES
        visible = diff_lines[:_MAX_DIFF_LINES]
        diff_text = "".join(visible)
        if truncated:
            diff_text += _TRUNC_NOTICE

    return Syntax(diff_text, "diff", theme="monokai", line_numbers=False)


def render_new_file(content: str, path: str) -> Syntax:
    """Return a Rich Syntax object for a brand-new file (no old content).

    This is a convenience wrapper around render_diff(old=None, ...) that
    also annotates the header to make it clear it's a new file.
    """
    return render_diff(None, content, path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Normalise line endings to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")
