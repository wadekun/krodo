"""Built-in filesystem tools: read_file, write_file, edit_file.

Path safety: every path is resolved via ctx.workspace.root.  No tool ever
calls Path.cwd() or __file__ (§3.4.2 invariant 2).

M3: edit_file caches the SHA-256 of the file at read-time and validates it
before writing (recovery scenario 5 — concurrent modification conflict).

M4: write_file and edit_file create a git stash checkpoint before writing
and emit a CHECKPOINT SessionEvent via ctx.event_logger.
"""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

from pydantic import BaseModel

from coda.core.types import SessionEventType, ToolDef, ToolResult
from coda.tools.protocols import ToolContext

_READ_LIMIT_BYTES = 50_000

# Per-instance SHA-256 cache: absolute_path_str → hex_digest
# Populated on read; validated before write in edit_file.
_sha256_cache: dict[str, str] = {}


def _compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_path(path: str, ctx: ToolContext) -> Path | str:
    """Resolve *path* relative to the workspace root.

    Returns the resolved ``Path`` on success, or an error string on failure.
    Callers should check ``isinstance(result, str)`` to detect errors.
    """
    try:
        target = (ctx.workspace.root / path).resolve()
    except (OSError, RuntimeError) as exc:
        return f"ERROR: cannot resolve path '{path}': {exc}"
    if not ctx.workspace.is_path_inside(target):
        return f"ERROR: path '{path}' resolves outside workspace root ({ctx.workspace.root})"
    return target


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class ReadFileParams(BaseModel):
    path: str
    offset: int | None = None
    limit: int | None = None


class ReadFileTool:
    definition = ToolDef(
        name="read_file",
        description=(
            "Read a UTF-8 text file inside the project workspace. "
            "path is relative to the workspace root. "
            "Use offset/limit (line numbers, 1-based) to read a slice."
        ),
        parameters=ReadFileParams,
    )
    requires_approval = False

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        params = ReadFileParams.model_validate(args)
        result_text = self._read(params, ctx)
        is_error = result_text.startswith("ERROR") or result_text.startswith("PathIgnoredError")
        return ToolResult(
            tool_call_id="",
            content=result_text,
            is_error=is_error,
        )

    def _read(self, params: ReadFileParams, ctx: ToolContext) -> str:
        target = _resolve_path(params.path, ctx)
        if isinstance(target, str):
            return target  # error string

        # Check CodaIgnore before reading (§5.3)
        match = ctx.ignore.match(target)
        if match.is_ignored:
            return str(match.error())

        if not target.exists():
            return f"ERROR: file '{params.path}' does not exist"
        if not target.is_file():
            return f"ERROR: '{params.path}' is not a regular file"

        try:
            raw = target.read_bytes()
        except OSError as exc:
            return f"ERROR: cannot read '{params.path}': {exc}"

        # Cache SHA-256 for conflict detection in edit_file (M3 recovery scenario 5)
        _sha256_cache[str(target)] = _compute_sha256(raw)

        if len(raw) > _READ_LIMIT_BYTES:
            raw = raw[:_READ_LIMIT_BYTES]
            suffix = "\n... [truncated — file exceeds read limit]"
        else:
            suffix = ""

        text = raw.decode("utf-8", errors="replace")

        if params.offset is not None or params.limit is not None:
            lines = text.splitlines(keepends=True)
            start = max(0, (params.offset or 1) - 1)
            end = start + params.limit if params.limit else len(lines)
            text = "".join(lines[start:end])

        return text + suffix


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class WriteFileParams(BaseModel):
    path: str
    content: str


class WriteFileTool:
    definition = ToolDef(
        name="write_file",
        description=(
            "Write or overwrite a UTF-8 text file inside the project workspace. "
            "path is relative to the workspace root. "
            "The caller must have approval before this tool executes."
        ),
        parameters=WriteFileParams,
    )
    requires_approval = True

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        params = WriteFileParams.model_validate(args)
        target = _resolve_path(params.path, ctx)
        if isinstance(target, str):
            return ToolResult(tool_call_id="", content=target, is_error=True)

        # Create checkpoint before writing (§5.4)
        sha = await ctx.checkpoint.create([target])
        if sha and ctx.event_logger is not None:
            from coda.core.events import SessionEventLogger  # noqa: PLC0415

            if isinstance(ctx.event_logger, SessionEventLogger):
                ctx.event_logger.emit(
                    SessionEventType.CHECKPOINT,
                    data={
                        "sha": sha,
                        "affected_paths": [str(target)],
                        "tool": "write_file",
                    },
                )

        result_text = self._write(params, ctx, target)
        return ToolResult(
            tool_call_id="",
            content=result_text,
            is_error=result_text.startswith("ERROR"),
        )

    def _write(self, params: WriteFileParams, ctx: ToolContext, target: Path) -> str:
        old_text = ""
        if target.exists():
            try:
                old_text = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
            diff = "\n".join(
                difflib.unified_diff(
                    old_text.splitlines(),
                    params.content.splitlines(),
                    fromfile=f"a/{params.path}",
                    tofile=f"b/{params.path}",
                    lineterm="",
                )
            )
            if diff:
                ctx.logger.info("write_file diff:\n%s", diff)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(params.content, encoding="utf-8")
        except OSError as exc:
            return f"ERROR: cannot write '{params.path}': {exc}"

        lines = len(params.content.splitlines())
        return f"OK: wrote {lines} lines to '{params.path}'"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


class EditFileParams(BaseModel):
    path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class EditFileTool:
    definition = ToolDef(
        name="edit_file",
        description=(
            "Edit a file by replacing an exact string with a new string. "
            "path is relative to the workspace root. "
            "old_string must appear exactly once in the file unless replace_all=true. "
            "For large or multi-location changes, prefer apply_patch instead. "
            "The caller must have approval before this tool executes."
        ),
        parameters=EditFileParams,
    )
    requires_approval = True

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        params = EditFileParams.model_validate(args)
        target = _resolve_path(params.path, ctx)
        if isinstance(target, str):
            return ToolResult(tool_call_id="", content=target, is_error=True)

        # Create checkpoint before editing (§5.4)
        sha = await ctx.checkpoint.create([target])
        if sha and ctx.event_logger is not None:
            from coda.core.events import SessionEventLogger  # noqa: PLC0415

            if isinstance(ctx.event_logger, SessionEventLogger):
                ctx.event_logger.emit(
                    SessionEventType.CHECKPOINT,
                    data={
                        "sha": sha,
                        "affected_paths": [str(target)],
                        "tool": "edit_file",
                    },
                )

        result_text = self._edit(params, ctx, target)
        return ToolResult(
            tool_call_id="",
            content=result_text,
            is_error=result_text.startswith("ERROR"),
        )

    def _edit(self, params: EditFileParams, ctx: ToolContext, target: Path) -> str:
        if not target.exists():
            return f"ERROR: file '{params.path}' does not exist"
        if not target.is_file():
            return f"ERROR: '{params.path}' is not a regular file"

        try:
            raw_bytes = target.read_bytes()
        except OSError as exc:
            return f"ERROR: cannot read '{params.path}': {exc}"

        # M3 recovery scenario 5: detect external modification via SHA-256
        current_sha256 = _compute_sha256(raw_bytes)
        cached_sha256 = _sha256_cache.get(str(target))
        if cached_sha256 is not None and current_sha256 != cached_sha256:
            # Update cache to reflect current on-disk state
            _sha256_cache[str(target)] = current_sha256
            return (
                f"ERROR: file '{params.path}' was modified externally since it was last read. "
                "Re-read the file to get its current contents before editing."
            )
        # Update cache for this read
        _sha256_cache[str(target)] = current_sha256

        original = raw_bytes.decode("utf-8", errors="replace")
        count = original.count(params.old_string)
        if count == 0:
            return (
                f"ERROR: old_string not found in '{params.path}'. "
                "Add more surrounding context to make it unique."
            )
        if count > 1 and not params.replace_all:
            # Report line numbers to help the model refine old_string
            lines_with_hit = [
                i + 1 for i, line in enumerate(original.splitlines()) if params.old_string in line
            ]
            return (
                f"ERROR: old_string appears {count} times in '{params.path}' "
                f"(lines containing match: {lines_with_hit}). "
                "Provide more context to make it unique, or set replace_all=true."
            )

        updated = (
            original.replace(params.old_string, params.new_string)
            if params.replace_all
            else original.replace(params.old_string, params.new_string, 1)
        )

        diff = "\n".join(
            difflib.unified_diff(
                original.splitlines(),
                updated.splitlines(),
                fromfile=f"a/{params.path}",
                tofile=f"b/{params.path}",
                lineterm="",
            )
        )
        if diff:
            ctx.logger.info("edit_file diff:\n%s", diff)

        try:
            target.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return f"ERROR: cannot write '{params.path}': {exc}"

        replacements = count if params.replace_all else 1
        return f"OK: replaced {replacements} occurrence(s) in '{params.path}'"
