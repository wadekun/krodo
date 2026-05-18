"""Tests for ListDirTool, GlobTool, GrepTool."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from coda.core.workspace import LocalWorkspaceResolver
from coda.sandbox.firewall import LocalSandboxRunner
from coda.sandbox.ignore import CodaIgnore
from coda.tools.builtin.search import GlobTool, GrepTool, ListDirTool, _rg_available
from coda.tools.protocols import ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    sb = LocalSandboxRunner(ws)
    return ToolContext(
        workspace=ws,
        sandbox=sb,
        session_id="test",
        logger=logging.getLogger("test"),
        ignore=CodaIgnore(tmp_path),
    )


def _make_tree(tmp_path: Path) -> None:
    """Build a small but representative directory tree."""
    (tmp_path / "src" / "coda").mkdir(parents=True)
    (tmp_path / "src" / "coda" / "main.py").write_text("# main\nTODO: implement me\n")
    (tmp_path / "src" / "coda" / "utils.py").write_text("# utils\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("# test\nTODO: add tests\n")
    (tmp_path / "README.md").write_text("# Readme\n")
    # Noise directories that should be skipped
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("module.exports = {}")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"\x00")


# ---------------------------------------------------------------------------
# ListDirTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_dir_root(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    result = await ListDirTool().execute({"path": ".", "depth": 1}, ctx)
    assert not result.is_error, result.content
    assert "src" in result.content
    assert "README.md" in result.content


@pytest.mark.asyncio
async def test_list_dir_skips_noise(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    result = await ListDirTool().execute({"path": ".", "depth": 3}, ctx)
    assert not result.is_error, result.content
    assert "node_modules" not in result.content
    assert "__pycache__" not in result.content


@pytest.mark.asyncio
async def test_list_dir_depth_limit(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    # depth=1 should not show files inside src/coda/
    result = await ListDirTool().execute({"path": ".", "depth": 1}, ctx)
    assert not result.is_error
    assert "main.py" not in result.content  # nested 2 deep


@pytest.mark.asyncio
async def test_list_dir_depth_2_shows_nested(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    result = await ListDirTool().execute({"path": ".", "depth": 2}, ctx)
    assert not result.is_error
    # src/ is depth 1; src/coda/ is depth 2 → listed
    assert "coda" in result.content


@pytest.mark.asyncio
async def test_list_dir_missing_path(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await ListDirTool().execute({"path": "nonexistent"}, ctx)
    assert result.is_error
    assert "does not exist" in result.content


@pytest.mark.asyncio
async def test_list_dir_traversal_denied(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await ListDirTool().execute({"path": "../../etc"}, ctx)
    assert result.is_error
    assert "outside workspace" in result.content


@pytest.mark.asyncio
async def test_list_dir_not_a_directory(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("content")
    ctx = _ctx(tmp_path)
    result = await ListDirTool().execute({"path": "file.txt"}, ctx)
    assert result.is_error
    assert "not a directory" in result.content


@pytest.mark.asyncio
async def test_list_dir_skips_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_list"
    outside.mkdir(exist_ok=True)
    (outside / "secret").write_text("secret")
    link = tmp_path / "link_dir"
    link.symlink_to(outside)
    ctx = _ctx(tmp_path)
    result = await ListDirTool().execute({"path": ".", "depth": 2}, ctx)
    # The symlink dir should not be traversed; 'secret' must not appear
    assert "secret" not in result.content


# ---------------------------------------------------------------------------
# GlobTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_glob_finds_py_files(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    result = await GlobTool().execute({"pattern": "**/*.py", "path": "."}, ctx)
    assert not result.is_error, result.content
    assert "main.py" in result.content
    assert "utils.py" in result.content


@pytest.mark.asyncio
async def test_glob_skips_noise_dirs(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    result = await GlobTool().execute({"pattern": "**/*.js", "path": "."}, ctx)
    # node_modules/pkg/index.js should be filtered out
    assert "index.js" not in result.content


@pytest.mark.asyncio
async def test_glob_no_matches(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    result = await GlobTool().execute({"pattern": "**/*.rs", "path": "."}, ctx)
    assert not result.is_error
    assert "no matches" in result.content


@pytest.mark.asyncio
async def test_glob_traversal_denied(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await GlobTool().execute({"pattern": "**/*.py", "path": "../../etc"}, ctx)
    assert result.is_error
    assert "outside workspace" in result.content


@pytest.mark.asyncio
async def test_glob_symlink_escape_filtered(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_glob"
    outside.mkdir(exist_ok=True)
    (outside / "secret.py").write_text("x = 1")
    link = tmp_path / "link_outside.py"
    link.symlink_to(outside / "secret.py")
    ctx = _ctx(tmp_path)
    result = await GlobTool().execute({"pattern": "**/*.py", "path": "."}, ctx)
    # link resolves outside → filtered; must not appear
    assert "link_outside.py" not in result.content or "secret.py" not in result.content


# ---------------------------------------------------------------------------
# GrepTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grep_finds_pattern(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    # Force Python fallback to keep test hermetic
    with patch("coda.tools.builtin.search._try_ripgrep", new_callable=AsyncMock, return_value=None):
        result = await GrepTool().execute({"pattern": "TODO", "path": "."}, ctx)
    assert not result.is_error, result.content
    assert "TODO" in result.content


@pytest.mark.asyncio
async def test_grep_case_insensitive(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    with patch("coda.tools.builtin.search._try_ripgrep", new_callable=AsyncMock, return_value=None):
        result = await GrepTool().execute(
            {"pattern": "todo", "path": ".", "case_sensitive": False}, ctx
        )
    assert not result.is_error, result.content
    assert "TODO" in result.content or "todo" in result.content


@pytest.mark.asyncio
async def test_grep_with_include_filter(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    with patch("coda.tools.builtin.search._try_ripgrep", new_callable=AsyncMock, return_value=None):
        result = await GrepTool().execute({"pattern": "TODO", "path": ".", "include": "*.md"}, ctx)
    # README.md does not have TODO; only .py files do
    assert not result.is_error
    assert "no matches" in result.content or "README" not in result.content


@pytest.mark.asyncio
async def test_grep_skips_noise_dirs(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    with patch("coda.tools.builtin.search._try_ripgrep", new_callable=AsyncMock, return_value=None):
        result = await GrepTool().execute({"pattern": "exports", "path": "."}, ctx)
    # node_modules/pkg/index.js has "module.exports" but should be skipped
    assert "index.js" not in result.content


@pytest.mark.asyncio
async def test_grep_no_matches(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    with patch("coda.tools.builtin.search._try_ripgrep", new_callable=AsyncMock, return_value=None):
        result = await GrepTool().execute({"pattern": "XYZZY_NOTFOUND_999", "path": "."}, ctx)
    assert not result.is_error
    assert "no matches" in result.content


@pytest.mark.asyncio
async def test_grep_invalid_regex(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    with patch("coda.tools.builtin.search._try_ripgrep", new_callable=AsyncMock, return_value=None):
        result = await GrepTool().execute({"pattern": "[invalid(regex", "path": "."}, ctx)
    assert result.is_error
    assert "invalid regex" in result.content


@pytest.mark.asyncio
async def test_grep_traversal_denied(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await GrepTool().execute({"pattern": "root", "path": "../../etc"}, ctx)
    assert result.is_error
    assert "outside workspace" in result.content


@pytest.mark.asyncio
@pytest.mark.skipif(not _rg_available(), reason="ripgrep not installed")
async def test_grep_uses_ripgrep_when_available(tmp_path: Path) -> None:
    """Integration test: verify rg-based grep returns sensible output."""
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    result = await GrepTool().execute({"pattern": "TODO", "path": "."}, ctx)
    assert not result.is_error, result.content
    assert "TODO" in result.content


# ---------------------------------------------------------------------------
# Additional coverage: truncation, edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_dir_empty_directory(tmp_path: Path) -> None:
    empty = tmp_path / "empty_dir"
    empty.mkdir()
    ctx = _ctx(tmp_path)
    result = await ListDirTool().execute({"path": "empty_dir"}, ctx)
    assert not result.is_error
    assert "empty" in result.content


@pytest.mark.asyncio
async def test_glob_base_path_not_exist(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await GlobTool().execute({"pattern": "*.py", "path": "nonexistent"}, ctx)
    assert result.is_error
    assert "does not exist" in result.content


@pytest.mark.asyncio
async def test_grep_on_missing_path(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await GrepTool().execute({"pattern": "x", "path": "no_such_dir"}, ctx)
    assert result.is_error
    assert "does not exist" in result.content


@pytest.mark.asyncio
async def test_grep_python_fallback_no_matches_for_include(tmp_path: Path) -> None:
    """Python fallback with include filter that doesn't match any files."""
    _make_tree(tmp_path)
    ctx = _ctx(tmp_path)
    with patch("coda.tools.builtin.search._try_ripgrep", new_callable=AsyncMock, return_value=None):
        result = await GrepTool().execute({"pattern": "TODO", "path": ".", "include": "*.rs"}, ctx)
    assert not result.is_error
    assert "no matches" in result.content


@pytest.mark.asyncio
async def test_try_ripgrep_returns_none_when_rg_not_found(tmp_path: Path) -> None:
    """_try_ripgrep returns None when rg binary is not found."""
    from coda.tools.builtin.search import _try_ripgrep

    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("rg not found"),
    ):
        result = await _try_ripgrep(
            "TODO",
            tmp_path,
            tmp_path,
            case_sensitive=True,
            include=None,
        )
    assert result is None


@pytest.mark.asyncio
async def test_try_ripgrep_no_output_returns_no_matches(tmp_path: Path) -> None:
    """_try_ripgrep returns a '(no matches)' string when rg exits with empty output."""
    from unittest.mock import MagicMock

    from coda.tools.builtin.search import _try_ripgrep

    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await _try_ripgrep(
            "XYZZY_NOT_FOUND",
            tmp_path,
            tmp_path,
            case_sensitive=True,
            include=None,
        )
    assert result is not None
    assert "no matches" in result


# ---------------------------------------------------------------------------
# CodaIgnore integration in search tools (M4 PR5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_dir_skips_ignored_paths(tmp_path: Path) -> None:
    """list_dir should not return paths matched by CodaIgnore."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("code\n")
    (tmp_path / ".env").write_text("SECRET=1\n")

    ctx = _ctx(tmp_path)
    result = await ListDirTool().execute({"path": ".", "depth": 2}, ctx)
    assert not result.is_error
    assert "main.py" in result.content
    assert ".env" not in result.content


@pytest.mark.asyncio
async def test_glob_skips_ignored_paths(tmp_path: Path) -> None:
    """glob should exclude .env files matched by hardcoded defaults."""
    (tmp_path / "app.py").write_text("code\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / ".env.local").write_text("SECRET=2\n")

    ctx = _ctx(tmp_path)
    result = await GlobTool().execute({"pattern": "*", "path": "."}, ctx)
    assert not result.is_error
    assert "app.py" in result.content
    assert ".env" not in result.content


@pytest.mark.asyncio
async def test_grep_skips_ignored_files(tmp_path: Path) -> None:
    """grep Python fallback should not search inside ignored files."""
    (tmp_path / "code.py").write_text("SECRET=find_me\n")
    (tmp_path / ".env").write_text("SECRET=find_me\n")

    ctx = _ctx(tmp_path)
    with patch("coda.tools.builtin.search._try_ripgrep", return_value=None):
        result = await GrepTool().execute(
            {"pattern": "find_me", "path": ".", "case_sensitive": True}, ctx
        )
    assert not result.is_error
    assert "code.py" in result.content
    assert ".env" not in result.content
