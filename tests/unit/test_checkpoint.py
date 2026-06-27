"""Tests for GitCheckpointManager (M4 PR2)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from krodo.core.workspace import Workspace
from krodo.sandbox.checkpoint import CheckpointError, GitCheckpointManager, shell_command_writes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(root: Path, git_root: Path | None = None) -> Workspace:
    return Workspace(
        root=root,
        config_path=root / ".krodo" / "config.yaml",
        memory_paths=[],
        git_root=git_root,
        source="cwd",
        discovered_at=datetime.now(tz=UTC),
    )


def _init_git(path: Path) -> None:
    """Initialise a minimal git repo."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)  # noqa: S603, S607
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        capture_output=True,
        check=True,
        cwd=str(path),
    )  # noqa: S603, S607
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        capture_output=True,
        check=True,
        cwd=str(path),
    )  # noqa: S603, S607


# ---------------------------------------------------------------------------
# shell_command_writes heuristic
# ---------------------------------------------------------------------------


class TestShellCommandWrites:
    def test_ls_is_read(self) -> None:
        assert not shell_command_writes("ls -la")

    def test_cat_is_read(self) -> None:
        assert not shell_command_writes("cat file.txt")

    def test_echo_redirect_is_write(self) -> None:
        assert shell_command_writes("echo foo > file.txt")

    def test_append_redirect_is_write(self) -> None:
        assert shell_command_writes("echo bar >> file.txt")

    def test_rm_is_write(self) -> None:
        assert shell_command_writes("rm -rf /tmp/foo")

    def test_mv_is_write(self) -> None:
        assert shell_command_writes("mv old.txt new.txt")

    def test_cp_is_write(self) -> None:
        assert shell_command_writes("cp src dst")

    def test_mkdir_is_write(self) -> None:
        assert shell_command_writes("mkdir -p /tmp/dir")

    def test_sed_inplace_is_write(self) -> None:
        assert shell_command_writes("sed -i 's/foo/bar/g' file.py")

    def test_tee_is_write(self) -> None:
        assert shell_command_writes("cat file | tee output.txt")

    def test_wget_is_write(self) -> None:
        assert shell_command_writes("wget https://example.com/file.zip")

    def test_git_checkout_is_write(self) -> None:
        assert shell_command_writes("git checkout main")

    def test_git_log_is_not_write(self) -> None:
        assert not shell_command_writes("git log --oneline")

    def test_pytest_is_not_write(self) -> None:
        assert not shell_command_writes("pytest tests/")

    def test_grep_is_not_write(self) -> None:
        assert not shell_command_writes("grep -r pattern .")


# ---------------------------------------------------------------------------
# GitCheckpointManager — non-git workspace
# ---------------------------------------------------------------------------


class TestNonGitWorkspace:
    @pytest.mark.asyncio
    async def test_create_returns_none_for_non_git(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path, git_root=None)
        mgr = GitCheckpointManager(ws)
        sha = await mgr.create([tmp_path / "file.py"])
        assert sha is None

    def test_restore_raises_for_non_git(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path, git_root=None)
        mgr = GitCheckpointManager(ws)
        with pytest.raises(CheckpointError, match="not inside a git repository"):
            mgr.restore("abc123", [tmp_path / "file.py"])

    def test_git_root_property_none(self, tmp_path: Path) -> None:
        ws = _make_workspace(tmp_path, git_root=None)
        mgr = GitCheckpointManager(ws)
        assert mgr.git_root is None


# ---------------------------------------------------------------------------
# GitCheckpointManager — git workspace (clean tree)
# ---------------------------------------------------------------------------


class TestGitWorkspaceClean:
    @pytest.mark.asyncio
    async def test_create_clean_tree_returns_none(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        # Commit a file so HEAD exists
        (tmp_path / "readme.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True, check=True)  # noqa: S603, S607
        subprocess.run(  # noqa: S603, S607
            ["git", "commit", "-m", "init"],
            cwd=str(tmp_path),
            capture_output=True,
            check=True,
        )

        ws = _make_workspace(tmp_path, git_root=tmp_path)
        mgr = GitCheckpointManager(ws)
        sha = await mgr.create([tmp_path / "readme.txt"])
        # Clean tree → git stash create returns nothing → None
        assert sha is None


# ---------------------------------------------------------------------------
# GitCheckpointManager — git workspace (dirty tree)
# ---------------------------------------------------------------------------


class TestGitWorkspaceDirty:
    @pytest.mark.asyncio
    async def test_create_dirty_tree_returns_sha(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        # Make an initial commit
        (tmp_path / "readme.txt").write_text("hello")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True, check=True)  # noqa: S603, S607
        subprocess.run(  # noqa: S603, S607
            ["git", "commit", "-m", "init"],
            cwd=str(tmp_path),
            capture_output=True,
            check=True,
        )
        # Make a dirty change
        (tmp_path / "readme.txt").write_text("modified")

        ws = _make_workspace(tmp_path, git_root=tmp_path)
        mgr = GitCheckpointManager(ws)
        sha = await mgr.create([tmp_path / "readme.txt"])
        assert sha is not None
        assert len(sha) == 40  # git SHA is 40 hex chars

    @pytest.mark.asyncio
    async def test_restore_reverts_file(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        f = tmp_path / "hello.py"
        f.write_text("original\n")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True, check=True)  # noqa: S603, S607
        subprocess.run(  # noqa: S603, S607
            ["git", "commit", "-m", "init"],
            cwd=str(tmp_path),
            capture_output=True,
            check=True,
        )

        # Modify file and create checkpoint
        f.write_text("modified\n")
        ws = _make_workspace(tmp_path, git_root=tmp_path)
        mgr = GitCheckpointManager(ws)
        sha = await mgr.create([f])
        assert sha is not None

        # Now write more changes and restore
        f.write_text("even more changes\n")
        mgr.restore(sha, [f])
        assert f.read_text() == "modified\n"

    def test_restore_bad_sha_raises(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True, check=True)  # noqa: S603, S607
        subprocess.run(  # noqa: S603, S607
            ["git", "commit", "-m", "init"],
            cwd=str(tmp_path),
            capture_output=True,
            check=True,
        )
        ws = _make_workspace(tmp_path, git_root=tmp_path)
        mgr = GitCheckpointManager(ws)
        with pytest.raises(CheckpointError):
            mgr.restore("0" * 40, [tmp_path / "f.txt"])


# ---------------------------------------------------------------------------
# GitCheckpointManager — timeout / OS error handling
# ---------------------------------------------------------------------------


class TestGitCheckpointErrors:
    @pytest.mark.asyncio
    async def test_timeout_returns_none(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        ws = _make_workspace(tmp_path, git_root=tmp_path)
        mgr = GitCheckpointManager(ws)
        with patch("asyncio.wait_for", side_effect=TimeoutError):
            sha = await mgr.create([tmp_path / "f.py"])
        assert sha is None

    @pytest.mark.asyncio
    async def test_oserror_returns_none(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        ws = _make_workspace(tmp_path, git_root=tmp_path)
        mgr = GitCheckpointManager(ws)
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("git not found")):
            sha = await mgr.create([tmp_path / "f.py"])
        assert sha is None
