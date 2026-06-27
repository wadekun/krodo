"""KrodoIgnore — 4-tier path filtering for all fs tools (architecture.md §5.3).

Merges ignore rules from four sources, in increasing specificity order:
    1. Hard-coded defaults (always active, cannot be disabled)
       — credential files (.env, *.pem, id_rsa*, …)
       — large/binary files (*.bin, *.so, *.zip, …)
       — noise directories (node_modules/, .venv/, __pycache__/, …)
    2. Project .gitignore (auto-respected)
    3. Project .krodoignore  (<workspace_root>/.krodoignore)
    4. User-level krodoignore (~/.config/krodo/krodoignore)

Matching uses the `pathspec` library (gitignore-spec, same semantics as
GitHub / VS Code).  All paths passed to `match()` must be relative to the
workspace root so that pathspec's directory-prefix logic works correctly.

Usage::

    ignore = KrodoIgnore.from_workspace(workspace)
    result = ignore.match(Path("config/.env"))
    if result.is_ignored:
        return ToolResult(is_error=True, content=str(result.error()))
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

import pathspec

if TYPE_CHECKING:
    from krodo.core.workspace import Workspace

# ---------------------------------------------------------------------------
# Hard-coded defaults (§5.3 §1 — cannot be disabled by any .krodoignore)
# ---------------------------------------------------------------------------

_HARDCODED_PATTERNS: list[str] = [
    # Credentials / secrets
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "credentials.json",
    "*.kdbx",
    "*.p12",
    "*.pfx",
    # Large / binary
    "*.bin",
    "*.so",
    "*.dylib",
    "*.dll",
    "*.exe",
    "*.zip",
    "*.tar",
    "*.tar.*",
    "*.gz",
    "*.bz2",
    "*.xz",
    "*.parquet",
    "*.sqlite",
    "*.db",
    "*.pkl",
    "*.pickle",
    "*.pyc",
    # Noise directories
    ".git/",
    "node_modules/",
    "__pycache__/",
    ".venv/",
    "venv/",
    "dist/",
    "build/",
    "target/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".pytest_cache/",
    "htmlcov/",
    ".tox/",
    ".eggs/",
]

# Source name constants for error messages
_SOURCE_HARDCODED = "hardcoded"
_SOURCE_GITIGNORE = ".gitignore"
_SOURCE_KRODOIGNORE = ".krodoignore"
_SOURCE_USER = "user-krodoignore"


# ---------------------------------------------------------------------------
# PathIgnoredError
# ---------------------------------------------------------------------------


class PathIgnoredError(Exception):
    """Raised (or returned as ToolResult error) when a path is blocked."""

    def __init__(self, path: Path, pattern: str, source: str) -> None:
        self.path = path
        self.pattern = pattern
        self.source = source
        super().__init__(f"PathIgnoredError: '{path}' is ignored (rule: '{pattern}' from {source})")


# ---------------------------------------------------------------------------
# MatchResult
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class MatchResult:
    """Returned by KrodoIgnore.match()."""

    is_ignored: bool
    path: Path
    pattern: str = ""
    source: str = ""

    def error(self) -> PathIgnoredError:
        """Return a PathIgnoredError for use in ToolResult.content."""
        return PathIgnoredError(self.path, self.pattern, self.source)


# ---------------------------------------------------------------------------
# KrodoIgnore
# ---------------------------------------------------------------------------


class KrodoIgnore:
    """4-tier ignore rule engine backed by pathspec (gitignore semantics).

    All tiers are stored as separate PathSpec objects so that the match source
    can be reported accurately in error messages.
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        extra_patterns: list[str] | None = None,
    ) -> None:
        self._root = workspace_root

        # Tier 1: hard-coded (always active)
        patterns = list(_HARDCODED_PATTERNS)
        if extra_patterns:
            patterns.extend(extra_patterns)
        self._hardcoded = pathspec.PathSpec.from_lines("gitwildmatch", patterns)

        # Tier 2: .gitignore
        self._gitignore = self._load_spec(workspace_root / ".gitignore")

        # Tier 3: .krodoignore
        self._krodoignore = self._load_spec(workspace_root / ".krodoignore")

        # Tier 4: user-level
        user_path = Path.home() / ".config" / "krodo" / "krodoignore"
        self._user = self._load_spec(user_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def match(self, path: Path, *, relative_to: Path | None = None) -> MatchResult:
        """Check whether *path* should be ignored.

        *path* may be absolute or relative; it is normalised to be relative to
        the workspace root before matching (pathspec requires this).

        Parameters
        ----------
        path:
            The path to check.
        relative_to:
            If provided, *path* is first made relative to this directory
            before being made relative to the workspace root.  Useful when
            tools receive paths relative to themselves rather than the root.
        """
        # Normalise to a workspace-relative PurePosixPath string
        try:
            if path.is_absolute():
                rel = path.relative_to(self._root)
            else:
                rel = path
        except ValueError:
            # Path outside workspace — let path firewall handle it
            return MatchResult(is_ignored=False, path=path)

        rel_str = rel.as_posix()

        # Check tiers in priority order: hardcoded → gitignore → krodoignore → user
        for spec, source in (
            (self._hardcoded, _SOURCE_HARDCODED),
            (self._gitignore, _SOURCE_GITIGNORE),
            (self._krodoignore, _SOURCE_KRODOIGNORE),
            (self._user, _SOURCE_USER),
        ):
            if spec is not None and spec.match_file(rel_str):
                # Extract the matching pattern for the error message
                pattern = self._find_pattern(spec, rel_str)
                return MatchResult(
                    is_ignored=True,
                    path=path,
                    pattern=pattern,
                    source=source,
                )

        return MatchResult(is_ignored=False, path=path)

    def is_ignored(self, path: Path) -> bool:
        """Convenience boolean check."""
        return self.match(path).is_ignored

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_workspace(cls, workspace: Workspace) -> KrodoIgnore:
        """Create a KrodoIgnore from a Workspace value object."""
        return cls(workspace.root)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_spec(path: Path) -> pathspec.PathSpec | None:
        """Load a PathSpec from a file, returning None if the file doesn't exist."""
        if not path.exists():
            return None
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return pathspec.PathSpec.from_lines("gitwildmatch", lines)
        except OSError:
            return None

    @staticmethod
    def _find_pattern(spec: pathspec.PathSpec, path_str: str) -> str:
        """Return the first matching pattern string from *spec*.

        pathspec does not expose which specific pattern matched, so we iterate.
        Falls back to '<unknown>' if iteration fails (edge case).
        """
        try:
            for pattern in spec.patterns:
                regex = getattr(pattern, "regex", None)
                if regex is not None and regex.search(path_str):
                    # Reconstruct original pattern text from the pattern object
                    raw = getattr(pattern, "_pattern", None) or getattr(pattern, "pattern", None)
                    if raw:
                        return str(raw)
        except Exception:  # noqa: BLE001, S110
            pass
        return "<unknown>"
