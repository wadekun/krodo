"""Built-in apply_patch tool.

Applies a unified diff (udiff) to one or more files inside the workspace.

Design:
- Uses ``unidiff`` (PatchSet) for udiff parsing.
- Atomic transaction: all target file contents are snapshotted before any
  write; any single hunk failure triggers a full rollback to the snapshots.
- Path firewall: every target path is validated against the workspace root
  before reading or writing.
- LF/CRLF normalisation: the patch engine operates on LF-normalised text
  but preserves the original line endings of each file when writing back.
- M3: SHA-256 conflict detection for files in the _sha256_cache (populated
  by read_file).  If a file was externally modified between read and patch,
  the apply is rejected before any write.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import unidiff  # type: ignore[import-untyped]
from pydantic import BaseModel

from coda.core.types import ToolDef, ToolResult
from coda.tools.protocols import ToolContext


class ApplyPatchParams(BaseModel):
    patch: str  # unified diff text (may span multiple files)


class ApplyPatchTool:
    definition = ToolDef(
        name="apply_patch",
        description=(
            "Apply a unified diff (udiff) patch to files in the workspace. "
            "patch must be a valid udiff string, possibly spanning multiple files. "
            "All changes are applied atomically: if any hunk fails, all files "
            "are restored to their original contents. "
            "Paths in the patch are resolved relative to the workspace root. "
            "The caller must have approval before this tool executes."
        ),
        parameters=ApplyPatchParams,
    )
    requires_approval = True

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        params = ApplyPatchParams.model_validate(args)
        result_text = self._apply(params, ctx)
        return ToolResult(
            tool_call_id="",
            content=result_text,
            is_error=result_text.startswith("ERROR"),
        )

    def _apply(self, params: ApplyPatchParams, ctx: ToolContext) -> str:  # noqa: C901
        try:
            patch_set = unidiff.PatchSet(params.patch)
        except unidiff.errors.UnidiffParseError as exc:
            return f"ERROR: failed to parse udiff: {exc}"

        if not patch_set:
            return "ERROR: patch is empty or contains no hunks"

        root = ctx.workspace.root

        # ------------------------------------------------------------------ #
        # Phase 1: validate all paths and snapshot current file contents
        # ------------------------------------------------------------------ #
        snapshots: dict[Path, str | None] = {}  # None = file did not exist

        for patched_file in patch_set:
            raw_path = _canonical_path(patched_file)
            target = _resolve_patch_path(raw_path, root)
            if isinstance(target, str):
                return target  # error string

            if patched_file.is_added_file:
                # New file: must not exist yet (or path must be inside workspace)
                snapshots[target] = None
            else:
                if not target.exists():
                    return (
                        f"ERROR: patch references '{raw_path}' but file does not exist. "
                        "If this is a new file, the patch header must use /dev/null as source."
                    )
                try:
                    # Read raw bytes for SHA-256 check, then decode for snapshot
                    raw_data = target.read_bytes()
                except OSError as exc:
                    return f"ERROR: cannot read '{raw_path}': {exc}"

                # M3 recovery scenario 5: detect external modification
                from coda.tools.builtin.fs import _sha256_cache

                current_sha256 = hashlib.sha256(raw_data).hexdigest()
                cached_sha256 = _sha256_cache.get(str(target))
                if cached_sha256 is not None and current_sha256 != cached_sha256:
                    _sha256_cache[str(target)] = current_sha256
                    return (
                        f"ERROR: file '{raw_path}' was modified externally since it was "
                        "last read. Re-read the file before applying the patch."
                    )
                _sha256_cache[str(target)] = current_sha256

                try:
                    # Decode for snapshot (preserve CRLF via newline="")
                    with target.open(encoding="utf-8", newline="") as fh:
                        snapshots[target] = fh.read()
                except OSError as exc:
                    return f"ERROR: cannot read '{raw_path}': {exc}"

        # ------------------------------------------------------------------ #
        # Phase 2: apply hunks (with rollback on any failure)
        # ------------------------------------------------------------------ #
        applied: list[Path] = []
        error: str | None = None

        for patched_file in patch_set:
            raw_path = _canonical_path(patched_file)
            target = _resolve_patch_path(raw_path, root)
            if isinstance(target, str):
                error = target
                break

            original = snapshots[target]

            if patched_file.is_removed_file:
                try:
                    target.unlink(missing_ok=True)
                    applied.append(target)
                except OSError as exc:
                    error = f"ERROR: cannot delete '{raw_path}': {exc}"
                    break
                continue

            # Normalise to LF for hunk application
            original_lf = (original or "").replace("\r\n", "\n").replace("\r", "\n")
            source_lines = original_lf.splitlines(keepends=True)

            try:
                result_lines = _apply_hunks(source_lines, patched_file)
            except _HunkError as exc:
                error = (
                    f"ERROR: hunk application failed in '{raw_path}': {exc}. "
                    "Ensure the patch context matches the actual file content."
                )
                break

            new_text = "".join(result_lines)

            # Restore original line endings if source used CRLF
            if original and "\r\n" in original:
                new_text = new_text.replace("\n", "\r\n")

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new_text, encoding="utf-8")
                applied.append(target)
            except OSError as exc:
                error = f"ERROR: cannot write '{raw_path}': {exc}"
                break

        # ------------------------------------------------------------------ #
        # Phase 3: rollback on error
        # ------------------------------------------------------------------ #
        if error:
            for path in applied:
                snap = snapshots.get(path)
                try:
                    if snap is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_text(snap, encoding="utf-8")
                except OSError:
                    pass  # best-effort rollback
            return error

        n = len(applied)
        return f"OK: patch applied successfully ({n} file(s) modified)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _HunkError(Exception):
    pass


def _canonical_path(patched_file: unidiff.PatchedFile) -> str:
    """Extract the canonical path from a PatchedFile, stripping a/ b/ prefixes."""
    # Prefer target path (b/); for removed files use source (a/)
    if patched_file.is_removed_file:
        raw = patched_file.source_file or ""
    else:
        raw = patched_file.target_file or patched_file.source_file or ""

    # Strip standard a/ b/ prefixes added by git diff
    for prefix in ("b/", "a/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    return raw


def _resolve_patch_path(raw_path: str, workspace_root: Path) -> Path | str:
    """Resolve *raw_path* against *workspace_root* with boundary check."""
    if not raw_path or raw_path in ("/dev/null",):
        return "ERROR: patch contains an invalid or null file path"
    try:
        target = (workspace_root / raw_path).resolve()
    except (OSError, RuntimeError) as exc:
        return f"ERROR: cannot resolve patch path '{raw_path}': {exc}"
    try:
        target.relative_to(workspace_root)
    except ValueError:
        return f"ERROR: patch path '{raw_path}' resolves outside workspace root ({workspace_root})"
    return target


def _apply_hunks(
    source_lines: list[str],
    patched_file: unidiff.PatchedFile,
) -> list[str]:
    """Apply all hunks from *patched_file* to *source_lines* (LF-only).

    Raises _HunkError if any hunk's context does not match.
    Returns the new list of lines (with newlines).
    """
    result: list[str] = list(source_lines)
    # Apply hunks in reverse order so line-number offsets stay valid
    hunks = list(patched_file)
    offset = 0

    for hunk in hunks:
        source_start = hunk.source_start - 1 + offset  # 0-based
        source_len = hunk.source_length

        # Verify context lines match
        context_end = source_start + source_len
        if context_end > len(result):
            raise _HunkError(
                f"hunk @@ -{hunk.source_start},{source_len} +{hunk.target_start},"
                f"{hunk.target_length} @@ extends beyond file length ({len(result)} lines)"
            )

        new_hunk_lines: list[str] = []
        src_idx = source_start

        for line in hunk:
            value = line.value
            if not value.endswith("\n"):
                value += "\n"

            if line.is_context:
                # Context must match the source file
                actual = result[src_idx] if src_idx < len(result) else ""
                if actual.rstrip("\n") != value.rstrip("\n"):
                    raise _HunkError(
                        f"context mismatch at source line {src_idx + 1}: "
                        f"expected {value!r}, got {actual!r}"
                    )
                new_hunk_lines.append(value)
                src_idx += 1
            elif line.is_removed:
                actual = result[src_idx] if src_idx < len(result) else ""
                if actual.rstrip("\n") != value.rstrip("\n"):
                    raise _HunkError(
                        f"removal mismatch at source line {src_idx + 1}: "
                        f"expected {value!r}, got {actual!r}"
                    )
                src_idx += 1
            elif line.is_added:
                new_hunk_lines.append(value)

        result[source_start:context_end] = new_hunk_lines
        offset += len(new_hunk_lines) - source_len

    return result
