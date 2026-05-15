"""Tests for GitStatusTool, GitDiffTool, GitCommitTool.

Each test creates a fresh temporary git repository using tmp_path + git init
so tests are hermetic and never touch the project's own .git directory.
"""

from __future__ import annotations

import logging
from pathlib import Path

import git as gitpython
import pytest

from coda.core.workspace import LocalWorkspaceResolver
from coda.sandbox.firewall import LocalSandboxRunner
from coda.tools.builtin.git import GitCommitTool, GitDiffTool, GitStatusTool, _redact_secrets
from coda.tools.protocols import ToolContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git_repo(tmp_path: Path) -> gitpython.Repo:
    """Create a minimal git repo with an initial commit in tmp_path."""
    repo = gitpython.Repo.init(str(tmp_path))
    # Configure git identity for the repo (needed for commits)
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "Test User")
        cfg.set_value("user", "email", "test@example.com")
    # Initial commit so HEAD exists
    readme = tmp_path / "README.md"
    readme.write_text("# Test\n")
    repo.index.add(["README.md"])
    repo.index.commit("Initial commit")
    return repo


def _ctx(tmp_path: Path) -> ToolContext:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    sb = LocalSandboxRunner(ws)
    return ToolContext(
        workspace=ws,
        sandbox=sb,
        session_id="test",
        logger=logging.getLogger("test"),
    )


def _ctx_no_git(tmp_path: Path) -> ToolContext:
    """Create a workspace context where git_root is None (not a git repo)."""
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    # Patch out git_root to simulate a non-git workspace
    object.__setattr__(ws, "git_root", None)
    sb = LocalSandboxRunner(ws)
    return ToolContext(
        workspace=ws,
        sandbox=sb,
        session_id="test",
        logger=logging.getLogger("test"),
    )


# ---------------------------------------------------------------------------
# _redact_secrets unit tests
# ---------------------------------------------------------------------------


def test_redact_secrets_openai_key() -> None:
    msg = "OPENAI_API_KEY=sk-proj-abc123 is in the message"
    assert "[REDACTED]" in _redact_secrets(msg)
    assert "sk-proj-abc123" not in _redact_secrets(msg)


def test_redact_secrets_sk_prefix() -> None:
    msg = "using sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx for auth"
    assert "[REDACTED]" in _redact_secrets(msg)
    assert "sk-ant-" not in _redact_secrets(msg)


def test_redact_secrets_clean_message() -> None:
    msg = "fix: update README with installation instructions"
    assert _redact_secrets(msg) == msg


def test_redact_secrets_multiple_keys() -> None:
    msg = "ANTHROPIC_API_KEY=sk-ant-abc OPENAI_API_KEY=sk-openai-xyz"
    redacted = _redact_secrets(msg)
    assert "sk-ant-abc" not in redacted
    assert "sk-openai-xyz" not in redacted
    assert redacted.count("[REDACTED]") >= 2


# ---------------------------------------------------------------------------
# GitStatusTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_status_clean(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    ctx = _ctx(tmp_path)
    result = await GitStatusTool().execute({}, ctx)
    assert not result.is_error, result.content
    assert "clean" in result.content or result.content == "(nothing to commit, working tree clean)"


@pytest.mark.asyncio
async def test_git_status_with_modified_file(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "README.md").write_text("# Modified\n")
    ctx = _ctx(tmp_path)
    result = await GitStatusTool().execute({}, ctx)
    assert not result.is_error, result.content
    assert "README.md" in result.content


@pytest.mark.asyncio
async def test_git_status_with_new_file(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "new_file.py").write_text("x = 1\n")
    ctx = _ctx(tmp_path)
    result = await GitStatusTool().execute({}, ctx)
    assert not result.is_error, result.content
    assert "new_file.py" in result.content


@pytest.mark.asyncio
async def test_git_status_no_git_repo(tmp_path: Path) -> None:
    ctx = _ctx_no_git(tmp_path)
    result = await GitStatusTool().execute({}, ctx)
    assert result.is_error
    assert "not inside a git repository" in result.content


# ---------------------------------------------------------------------------
# GitDiffTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_diff_no_changes(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    ctx = _ctx(tmp_path)
    result = await GitDiffTool().execute({}, ctx)
    assert not result.is_error, result.content
    assert "no working tree changes" in result.content


@pytest.mark.asyncio
async def test_git_diff_with_change(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "README.md").write_text("# Modified\n")
    ctx = _ctx(tmp_path)
    result = await GitDiffTool().execute({}, ctx)
    assert not result.is_error, result.content
    assert "README.md" in result.content
    assert "Modified" in result.content


@pytest.mark.asyncio
async def test_git_diff_staged(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    (tmp_path / "README.md").write_text("# Staged change\n")
    repo.index.add(["README.md"])
    ctx = _ctx(tmp_path)
    result = await GitDiffTool().execute({"staged": True}, ctx)
    assert not result.is_error, result.content
    assert "Staged change" in result.content


@pytest.mark.asyncio
async def test_git_diff_with_path_filter(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    (tmp_path / "README.md").write_text("# changed\n")
    (tmp_path / "other.py").write_text("x = 1\n")
    ctx = _ctx(tmp_path)
    result = await GitDiffTool().execute({"path": "README.md"}, ctx)
    assert not result.is_error, result.content


@pytest.mark.asyncio
async def test_git_diff_path_traversal_denied(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    ctx = _ctx(tmp_path)
    result = await GitDiffTool().execute({"path": "../../etc/passwd"}, ctx)
    assert result.is_error
    assert "outside workspace" in result.content


@pytest.mark.asyncio
async def test_git_diff_no_git_repo(tmp_path: Path) -> None:
    ctx = _ctx_no_git(tmp_path)
    result = await GitDiffTool().execute({}, ctx)
    assert result.is_error
    assert "not inside a git repository" in result.content


# ---------------------------------------------------------------------------
# GitCommitTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_commit_success(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    (tmp_path / "new.py").write_text("x = 1\n")
    repo.index.add(["new.py"])
    ctx = _ctx(tmp_path)
    result = await GitCommitTool().execute({"message": "feat: add new.py", "add_all": False}, ctx)
    assert not result.is_error, result.content
    assert "feat: add new.py" in result.content


@pytest.mark.asyncio
async def test_git_commit_add_all(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    (tmp_path / "README.md").write_text("# Updated\n")
    ctx = _ctx(tmp_path)
    result = await GitCommitTool().execute({"message": "docs: update README", "add_all": True}, ctx)
    assert not result.is_error, result.content
    # Verify commit was created
    assert repo.head.commit.message.strip() == "docs: update README"


@pytest.mark.asyncio
async def test_git_commit_nothing_staged(tmp_path: Path) -> None:
    _git_repo(tmp_path)
    ctx = _ctx(tmp_path)
    result = await GitCommitTool().execute({"message": "empty commit"}, ctx)
    assert result.is_error
    assert "nothing staged" in result.content


@pytest.mark.asyncio
async def test_git_commit_redacts_api_key(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    (tmp_path / "f.py").write_text("x = 1\n")
    repo.index.add(["f.py"])
    ctx = _ctx(tmp_path)
    result = await GitCommitTool().execute(
        {"message": "fix: remove OPENAI_API_KEY=sk-test-12345 from code"},
        ctx,
    )
    assert not result.is_error, result.content
    # Commit message must be redacted
    actual_msg = repo.head.commit.message
    assert "sk-test-12345" not in actual_msg
    assert "[REDACTED]" in actual_msg


@pytest.mark.asyncio
async def test_git_commit_no_git_repo(tmp_path: Path) -> None:
    ctx = _ctx_no_git(tmp_path)
    result = await GitCommitTool().execute({"message": "test"}, ctx)
    assert result.is_error
    assert "not inside a git repository" in result.content
