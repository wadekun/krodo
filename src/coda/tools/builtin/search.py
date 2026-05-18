"""Built-in search tools: list_dir, glob, grep.

All tools enforce the workspace path firewall and skip noise directories
(_NOISE_DIRS) without requiring .codaignore (that is deferred to M4).

grep uses ripgrep (rg) when available for ~100x faster search; falls back
to a pure-Python re implementation if rg is not installed.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from coda.core.types import ToolDef, ToolResult
from coda.sandbox.path_filter import _NOISE_DIRS, filter_allowed_paths
from coda.tools.protocols import ToolContext

if TYPE_CHECKING:
    pass

_MAX_RESULTS = 500  # cap returned entries to avoid token explosion
_MAX_GREP_BYTES = 200_000  # ripgrep/fallback output cap


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


class ListDirParams(BaseModel):
    path: str = "."
    depth: int = Field(default=1, ge=1, le=10)


class ListDirTool:
    definition = ToolDef(
        name="list_dir",
        description=(
            "List files and directories inside the workspace. "
            "path is relative to the workspace root (default: '.'). "
            "depth controls how many levels deep to recurse (1–10, default 1). "
            "Noise directories (.git, node_modules, __pycache__, etc.) are skipped."
        ),
        parameters=ListDirParams,
    )
    requires_approval = False

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        params = ListDirParams.model_validate(args)
        result_text = self._list(params, ctx)
        return ToolResult(
            tool_call_id="",
            content=result_text,
            is_error=result_text.startswith("ERROR"),
        )

    def _list(self, params: ListDirParams, ctx: ToolContext) -> str:
        try:
            base = (ctx.workspace.root / params.path).resolve()
        except (OSError, RuntimeError) as exc:
            return f"ERROR: cannot resolve path '{params.path}': {exc}"

        if not ctx.workspace.is_path_inside(base):
            return (
                f"ERROR: path '{params.path}' resolves outside workspace root "
                f"({ctx.workspace.root})"
            )
        if not base.exists():
            return f"ERROR: path '{params.path}' does not exist"
        if not base.is_dir():
            return f"ERROR: '{params.path}' is not a directory"

        entries = self._collect(base, ctx.workspace.root, params.depth, ctx)
        if not entries:
            return f"(empty directory: {params.path})"

        lines = []
        for rel in entries[:_MAX_RESULTS]:
            suffix = "/" if (base.parent / rel).is_dir() else ""
            lines.append(f"{rel}{suffix}")
        truncated = len(entries) > _MAX_RESULTS
        result = "\n".join(lines)
        if truncated:
            result += f"\n... [truncated — showing first {_MAX_RESULTS} of {len(entries)}]"
        return result

    def _collect(self, base: Path, root: Path, depth: int, ctx: ToolContext) -> list[str]:
        results: list[str] = []
        self._recurse(base, base, root, depth, 0, results, ctx)
        return results

    def _recurse(
        self,
        current: Path,
        base: Path,
        root: Path,
        max_depth: int,
        current_depth: int,
        results: list[str],
        ctx: ToolContext,
    ) -> None:
        if current_depth >= max_depth:
            return
        try:
            children = sorted(current.iterdir())
        except PermissionError:
            return
        for child in children:
            # Skip noise dirs by name
            if child.name in _NOISE_DIRS:
                continue
            # Skip symlinks that escape the workspace
            try:
                resolved = child.resolve()
            except (OSError, RuntimeError):
                continue
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            # Skip ignored paths
            if ctx.ignore.is_ignored(resolved):
                continue
            rel = str(resolved.relative_to(root))
            results.append(rel)
            if child.is_dir() and not child.is_symlink():
                self._recurse(child, base, root, max_depth, current_depth + 1, results, ctx)


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


class GlobParams(BaseModel):
    pattern: str
    path: str = "."


class GlobTool:
    definition = ToolDef(
        name="glob",
        description=(
            "Find files matching a glob pattern within the workspace. "
            "pattern uses pathlib glob syntax (e.g. '**/*.py', 'src/**/*.ts'). "
            "path is the base directory relative to workspace root (default: '.'). "
            "Noise directories (.git, node_modules, etc.) are skipped."
        ),
        parameters=GlobParams,
    )
    requires_approval = False

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        params = GlobParams.model_validate(args)
        result_text = self._glob(params, ctx)
        return ToolResult(
            tool_call_id="",
            content=result_text,
            is_error=result_text.startswith("ERROR"),
        )

    def _glob(self, params: GlobParams, ctx: ToolContext) -> str:
        try:
            base = (ctx.workspace.root / params.path).resolve()
        except (OSError, RuntimeError) as exc:
            return f"ERROR: cannot resolve path '{params.path}': {exc}"

        if not ctx.workspace.is_path_inside(base):
            return (
                f"ERROR: path '{params.path}' resolves outside workspace root "
                f"({ctx.workspace.root})"
            )
        if not base.exists():
            return f"ERROR: path '{params.path}' does not exist"

        try:
            raw_matches = list(base.rglob(params.pattern))
        except (OSError, ValueError) as exc:
            return f"ERROR: glob failed: {exc}"

        allowed = filter_allowed_paths(raw_matches, ctx.workspace)
        allowed = [p for p in allowed if not ctx.ignore.is_ignored(p)]
        if not allowed:
            return f"(no matches for '{params.pattern}' under '{params.path}')"

        root = ctx.workspace.root
        lines = [str(p.relative_to(root)) for p in sorted(allowed)[:_MAX_RESULTS]]
        truncated = len(allowed) > _MAX_RESULTS
        result = "\n".join(lines)
        if truncated:
            result += f"\n... [truncated — showing first {_MAX_RESULTS} of {len(allowed)}]"
        return result


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


class GrepParams(BaseModel):
    pattern: str
    path: str = "."
    case_sensitive: bool = True
    include: str | None = None  # file glob filter, e.g. "*.py"


class GrepTool:
    definition = ToolDef(
        name="grep",
        description=(
            "Search for a regex pattern in files within the workspace. "
            "pattern is a regular expression. "
            "path is relative to workspace root (default: '.'). "
            "include filters by filename glob (e.g. '*.py'). "
            "Uses ripgrep (rg) when available for best performance; "
            "falls back to Python re otherwise. "
            "Recommend installing ripgrep for large codebases (100x faster)."
        ),
        parameters=GrepParams,
    )
    requires_approval = False

    async def execute(self, args: dict[str, object], ctx: ToolContext) -> ToolResult:
        params = GrepParams.model_validate(args)
        result_text = await self._grep(params, ctx)
        return ToolResult(
            tool_call_id="",
            content=result_text,
            is_error=result_text.startswith("ERROR"),
        )

    async def _grep(self, params: GrepParams, ctx: ToolContext) -> str:
        try:
            base = (ctx.workspace.root / params.path).resolve()
        except (OSError, RuntimeError) as exc:
            return f"ERROR: cannot resolve path '{params.path}': {exc}"

        if not ctx.workspace.is_path_inside(base):
            return (
                f"ERROR: path '{params.path}' resolves outside workspace root "
                f"({ctx.workspace.root})"
            )
        if not base.exists():
            return f"ERROR: path '{params.path}' does not exist"

        result = await _run_ripgrep_or_fallback(
            params.pattern,
            base,
            ctx.workspace.root,
            case_sensitive=params.case_sensitive,
            include=params.include,
            ctx=ctx,
        )
        return result


async def _run_ripgrep_or_fallback(
    pattern: str,
    base: Path,
    workspace_root: Path,
    *,
    case_sensitive: bool = True,
    include: str | None = None,
    ctx: ToolContext | None = None,
) -> str:
    """Try ripgrep first; fall back to pure-Python re search."""
    rg_result = await _try_ripgrep(
        pattern, base, workspace_root, case_sensitive=case_sensitive, include=include
    )
    if rg_result is not None:
        return rg_result
    return _python_grep(
        pattern, base, workspace_root, case_sensitive=case_sensitive, include=include, ctx=ctx
    )


async def _try_ripgrep(
    pattern: str,
    base: Path,
    workspace_root: Path,
    *,
    case_sensitive: bool,
    include: str | None,
) -> str | None:
    """Return ripgrep output, or None if rg is not available."""
    cmd = ["rg", "--line-number", "--no-heading", "--color=never"]
    if not case_sensitive:
        cmd.append("--ignore-case")
    if include:
        cmd.extend(["--glob", include])
    # Skip noise dirs
    for nd in _NOISE_DIRS:
        cmd.extend(["--glob", f"!{nd}/**"])
    cmd.append(pattern)
    cmd.append(str(base))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (TimeoutError, FileNotFoundError, OSError):
        return None  # rg not found or timed out → use fallback

    raw = stdout[:_MAX_GREP_BYTES].decode("utf-8", errors="replace")
    if not raw.strip():
        return f"(no matches for '{pattern}')"

    # Make paths relative to workspace root
    lines = []
    for line in raw.splitlines():
        # rg output: /abs/path/file.py:42:match text
        if line.startswith(str(base)):
            rel = line[len(str(workspace_root)) + 1 :]
            lines.append(rel)
        else:
            lines.append(line)
    return "\n".join(lines[:_MAX_RESULTS])


def _python_grep(
    pattern: str,
    base: Path,
    workspace_root: Path,
    *,
    case_sensitive: bool,
    include: str | None,
    ctx: ToolContext | None = None,
) -> str:
    """Pure-Python grep fallback using pathlib.rglob + re."""
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return f"ERROR: invalid regex '{pattern}': {exc}"

    file_pattern = include if include else "*"
    matches: list[str] = []

    for path in sorted(base.rglob(file_pattern)):
        if not path.is_file():
            continue
        # Skip noise dirs
        try:
            rel = path.relative_to(workspace_root)
        except ValueError:
            continue
        if any(part in _NOISE_DIRS for part in rel.parts):
            continue
        # Skip paths ignored by CodaIgnore
        if ctx is not None and ctx.ignore.is_ignored(path):
            continue
        # Only text files
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                rel_str = str(rel)
                matches.append(f"{rel_str}:{lineno}:{line}")
                if len(matches) >= _MAX_RESULTS:
                    matches.append(f"... [truncated at {_MAX_RESULTS} matches]")
                    return "\n".join(matches)

    if not matches:
        return f"(no matches for '{pattern}')"
    return "\n".join(matches)


def _rg_available() -> bool:
    """Check if ripgrep is installed (used in tests)."""
    try:
        subprocess.run(["rg", "--version"], capture_output=True, check=True)  # noqa: S603, S607
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False
