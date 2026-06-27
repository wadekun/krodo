"""Workspace — the single source of truth for the project root and all path-based
operations.

A Workspace is constructed once at process start (CLI entry point) and injected
everywhere via ToolContext.  No subsystem is allowed to call Path.cwd() or use
__file__ to infer the root (architecture.md §3.4.2 invariant 2).

LocalWorkspaceResolver implements the 5-level discovery priority defined in
§3.4.2:
    1. CLI flag    --root <path>       → source="flag"
    2. Env var     KRODO_ROOT           → source="env"
    3. Ancestor with .krodo/ directory  → source="marker"
    4. Ancestor with .git/ directory   → source="marker"
    5. Current working directory       → source="cwd"
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, model_validator

WorkspaceSource = Literal["flag", "env", "marker", "cwd"]


class Workspace(BaseModel):
    """Frozen value object — constructed once, never mutated during a session."""

    model_config = {"frozen": True}

    root: Path
    config_path: Path
    memory_paths: list[Path]
    git_root: Path | None
    source: WorkspaceSource
    discovered_at: datetime

    @model_validator(mode="after")
    def _check_invariants(self) -> Workspace:
        if not self.root.is_dir():
            raise ValueError(f"Workspace root {self.root!r} is not a directory")
        if not os.access(self.root, os.W_OK):
            raise ValueError(f"Workspace root {self.root!r} is not writable")
        return self

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def is_path_inside(self, path: Path) -> bool:
        """Return True iff *path* (already resolved) is under workspace root."""
        try:
            path.relative_to(self.root)
            return True
        except ValueError:
            return False

    def discover_subdir_agents_md(self, cwd: Path) -> list[tuple[Path, str]]:
        """Walk *cwd* up to *self.root*, collecting intermediate AGENTS.md files.

        Returns outer-to-inner order (deepest last), excluding the project-root
        AGENTS.md (tier 2) which is handled separately by ``load_agents_md``.

        Delegates to :func:`krodo.memory.agents_md.discover_subdir_agents_md`
        to keep the discovery logic co-located with the rest of the merge code.
        """
        from krodo.memory.agents_md import discover_subdir_agents_md  # noqa: PLC0415

        return discover_subdir_agents_md(self, cwd)


@runtime_checkable
class WorkspaceResolver(Protocol):
    def resolve(self, explicit: Path | None) -> Workspace: ...


# ---------------------------------------------------------------------------
# LocalWorkspaceResolver — the only production implementation
# ---------------------------------------------------------------------------


class LocalWorkspaceResolver:
    """Discover the project root according to §3.4.2 priority chain."""

    def resolve(self, explicit: Path | None = None) -> Workspace:
        root, source = self._discover(explicit)

        config_path = root / ".krodo" / "config.yaml"
        memory_paths = self._collect_memory_paths(root)
        git_root = self._find_git_root(root)

        return Workspace(
            root=root,
            config_path=config_path,
            memory_paths=memory_paths,
            git_root=git_root,
            source=source,
            discovered_at=datetime.now(tz=UTC),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _discover(self, explicit: Path | None) -> tuple[Path, WorkspaceSource]:
        # Priority 1: explicit flag
        if explicit is not None:
            return explicit.expanduser().resolve(), "flag"

        # Priority 2: KRODO_ROOT env var
        env_root = os.environ.get("KRODO_ROOT")
        if env_root:
            return Path(env_root).expanduser().resolve(), "env"

        cwd = Path.cwd().resolve()

        # Priority 3: ancestor containing .krodo/
        marker = self._find_ancestor_with(cwd, ".krodo")
        if marker is not None:
            return marker, "marker"

        # Priority 4: ancestor containing .git/
        marker = self._find_ancestor_with(cwd, ".git")
        if marker is not None:
            return marker, "marker"

        # Priority 5: current working directory
        return cwd, "cwd"

    @staticmethod
    def _find_ancestor_with(start: Path, name: str) -> Path | None:
        for parent in [start, *start.parents]:
            if (parent / name).exists():
                return parent
        return None

    @staticmethod
    def _collect_memory_paths(root: Path) -> list[Path]:
        candidates = [
            root / "AGENTS.md",
            Path.home() / ".config" / "krodo" / "AGENTS.md",
        ]
        return [p for p in candidates if p.exists()]

    @staticmethod
    def _find_git_root(root: Path) -> Path | None:
        for parent in [root, *root.parents]:
            if (parent / ".git").exists():
                return parent
        return None
