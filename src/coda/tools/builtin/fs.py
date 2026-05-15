"""Built-in filesystem tools: read_file, write_file.

Path safety: every path is resolved via ctx.workspace.root.  No tool ever
calls Path.cwd() or __file__ (§3.4.2 invariant 2).
"""

from __future__ import annotations

import difflib
from pathlib import Path

from pydantic import BaseModel

from coda.core.types import ToolDef, ToolResult
from coda.tools.protocols import ToolContext

_READ_LIMIT_BYTES = 50_000


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
        return ToolResult(
            tool_call_id="",
            content=result_text,
            is_error=result_text.startswith("ERROR"),
        )

    def _read(self, params: ReadFileParams, ctx: ToolContext) -> str:
        target = self._resolve(params.path, ctx)
        if isinstance(target, str):
            return target  # error string

        if not target.exists():
            return f"ERROR: file '{params.path}' does not exist"
        if not target.is_file():
            return f"ERROR: '{params.path}' is not a regular file"

        try:
            raw = target.read_bytes()
        except OSError as exc:
            return f"ERROR: cannot read '{params.path}': {exc}"

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

    @staticmethod
    def _resolve(path: str, ctx: ToolContext) -> Path | str:
        try:
            target = (ctx.workspace.root / path).resolve()
        except (OSError, RuntimeError) as exc:
            return f"ERROR: cannot resolve path '{path}': {exc}"
        if not ctx.workspace.is_path_inside(target):
            return f"ERROR: path '{path}' resolves outside workspace root ({ctx.workspace.root})"
        return target


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
        result_text = self._write(params, ctx)
        return ToolResult(
            tool_call_id="",
            content=result_text,
            is_error=result_text.startswith("ERROR"),
        )

    def _write(self, params: WriteFileParams, ctx: ToolContext) -> str:
        target = self._resolve(params.path, ctx)
        if isinstance(target, str):
            return target

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

    @staticmethod
    def _resolve(path: str, ctx: ToolContext) -> Path | str:
        try:
            target = (ctx.workspace.root / path).resolve()
        except (OSError, RuntimeError) as exc:
            return f"ERROR: cannot resolve path '{path}': {exc}"
        if not ctx.workspace.is_path_inside(target):
            return f"ERROR: path '{path}' resolves outside workspace root ({ctx.workspace.root})"
        return target
