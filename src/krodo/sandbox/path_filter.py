"""Path filtering utilities for bulk-result tools (glob, list_dir, grep).

Unlike single-path checks in LocalSandboxRunner.is_path_allowed(), these
helpers process lists of paths returned by search operations and silently
drop any that escape the workspace boundary or are inside noise directories.

Both functions are pure (no I/O side-effects) and accept the Workspace value
object so callers never need to re-derive the root themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from krodo.core.workspace import Workspace

# Directories that are almost never useful for a coding agent to index.
# .krodoignore full loading is deferred to M4; this is the M2 hard-coded set.
_NOISE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".env",
        "dist",
        "build",
        "target",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "htmlcov",
        ".tox",
        ".eggs",
    }
)


def is_noise_dir(path: Path) -> bool:
    """Return True if *path* or any of its ancestors is a noise directory.

    Only checks the **name** components, not the full resolved path, so it is
    fast and does not require the path to exist on disk.
    """
    return any(part in _NOISE_DIRS for part in path.parts)


def filter_allowed_paths(paths: list[Path], workspace: Workspace) -> list[Path]:
    """Return a filtered copy of *paths* containing only paths that:

    1. Are inside *workspace.root* after symlink resolution.
    2. Are not inside a noise directory (checked by name, not resolved path).

    Silently drops disallowed paths — callers (GlobTool, ListDirTool, GrepTool)
    should log a summary count if useful, but must not crash on filtered paths.
    """
    allowed: list[Path] = []
    root = workspace.root
    for p in paths:
        # Resolve symlinks to catch escapes (mirrors is_path_allowed logic)
        try:
            resolved = p.resolve()
        except (OSError, RuntimeError):
            continue
        # Must be inside workspace root
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        # Drop noise directories
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        if is_noise_dir(rel):
            continue
        allowed.append(resolved)
    return allowed
