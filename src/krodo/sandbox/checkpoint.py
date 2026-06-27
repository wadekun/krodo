"""GitCheckpointManager — write-before safety net (architecture.md §5.4).

Every write tool (write_file, edit_file, apply_patch) and any shell command
that looks like it will modify the filesystem calls ``create()`` before
touching the disk.  The checkpoint is a ``git stash create`` ref — it never
enters the stash stack, so it never pollutes the user's stash list.

On non-git workspaces, ``create()`` degrades gracefully to a no-op (returns
``None`` and logs a warning).  ``restore()`` raises ``CheckpointError`` when
called on a non-git workspace.

``shell_command_writes(cmd)`` is a heuristic that returns ``True`` when *cmd*
looks like it will modify the filesystem.  It is intentionally conservative:
false-positives (unnecessary checkpoints) are safer than false-negatives.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging

    from krodo.core.workspace import Workspace

# ---------------------------------------------------------------------------
# Heuristic: detect write-likely shell commands
# ---------------------------------------------------------------------------

# Patterns whose presence anywhere in the command signals a write
_WRITE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r">"),  # stdout redirect (append or overwrite)
    re.compile(r"\bmv\b"),  # move / rename
    re.compile(r"\brm\b"),  # remove
    re.compile(r"\bcp\b"),  # copy (creates / overwrites destination)
    re.compile(r"\bmkdir\b"),  # create directory
    re.compile(r"\btouch\b"),  # create / update file
    re.compile(r"\bchmod\b"),  # may imply file ops
    re.compile(r"\bchown\b"),
    re.compile(r"\bsed\b.*-i"),  # in-place edit
    re.compile(r"\bawk\b.*>"),  # awk redirect
    re.compile(r"\btee\b"),  # tee writes
    re.compile(r"\binstall\b"),  # install command
    re.compile(r"\bcurl\b.*-o"),  # curl output to file
    re.compile(r"\bwget\b"),  # wget creates files
    re.compile(r"\btar\b.*[xX]"),  # extract archive
    re.compile(r"\bunzip\b"),  # extract zip
    re.compile(r"\bpatch\b"),  # apply patch
    re.compile(r"\bgit\b.*(checkout|reset|revert|apply|am|cherry-pick|merge|rebase)"),
]


def shell_command_writes(cmd: str) -> bool:
    """Return True when *cmd* is likely to modify the filesystem.

    This is a heuristic: false-positives create extra (harmless) checkpoints;
    false-negatives could miss changes.  Err on the side of safety.
    """
    return any(pat.search(cmd) for pat in _WRITE_PATTERNS)


# ---------------------------------------------------------------------------
# CheckpointError
# ---------------------------------------------------------------------------


class CheckpointError(Exception):
    """Raised when checkpoint operations cannot proceed."""


# ---------------------------------------------------------------------------
# GitCheckpointManager
# ---------------------------------------------------------------------------


class GitCheckpointManager:
    """Creates and restores ``git stash create`` checkpoints.

    Parameters
    ----------
    workspace:
        The active session workspace.  ``workspace.git_root`` determines
        whether git operations are available.
    logger:
        Injected logger; must not be ``None`` at runtime (optional only for
        unit-test convenience).
    """

    def __init__(self, workspace: Workspace, logger: logging.Logger | None = None) -> None:
        self._workspace = workspace
        self._logger = logger
        self._git_root: Path | None = workspace.git_root

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(self, affected_paths: list[Path]) -> str | None:
        """Create a stash checkpoint **without** pushing to the stash stack.

        Returns the stash SHA on success, or ``None`` when:
        - the workspace has no git root (non-git directory); or
        - the working tree is clean (nothing to stash).

        Parameters
        ----------
        affected_paths:
            Informational list of paths that will be written.  Stored in the
            event payload; not used by git directly.
        """
        if self._git_root is None:
            if self._logger:
                paths_str = [str(p) for p in affected_paths]
                self._logger.warning(
                    "checkpoint_skipped: not a git repository, affected_paths=%s", paths_str
                )
            return None

        sha = await self._git_stash_create()
        if sha:
            if self._logger:
                paths_str = [str(p) for p in affected_paths]
                self._logger.info("checkpoint_created: sha=%s affected_paths=%s", sha, paths_str)
        else:
            if self._logger:
                self._logger.debug("checkpoint_skipped: clean working tree")
        return sha or None

    def restore(self, sha: str, paths: list[Path]) -> None:
        """Restore *paths* to their state at the given stash SHA.

        Uses ``git checkout <sha> -- <paths>`` so that only the listed paths
        are affected; the rest of the working tree is untouched.

        Raises
        ------
        CheckpointError
            When the workspace has no git root, the SHA is unknown, or the
            git command fails.
        """
        if self._git_root is None:
            raise CheckpointError("Cannot restore: workspace is not inside a git repository.")

        str_paths = [str(p) for p in paths]
        cmd = ["git", "checkout", sha, "--"] + str_paths
        try:
            import subprocess  # noqa: PLC0415

            result = subprocess.run(  # noqa: S603
                cmd,
                cwd=str(self._git_root),
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise CheckpointError(f"git checkout failed: {exc}") from exc

        if result.returncode != 0:
            raise CheckpointError(
                f"git checkout failed (exit {result.returncode}): {result.stderr.strip()}"
            )

        if self._logger:
            self._logger.info("checkpoint_restored: sha=%s paths=%s", sha, str_paths)

    # ------------------------------------------------------------------
    # Property
    # ------------------------------------------------------------------

    @property
    def git_root(self) -> Path | None:
        return self._git_root

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _git_stash_create(self) -> str:
        """Run ``git stash create`` and return the SHA (or empty string)."""
        assert self._git_root is not None  # noqa: S101 (guarded by callers)
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "stash",
                "create",
                cwd=str(self._git_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except (TimeoutError, OSError):
            return ""
        return stdout.decode().strip()
