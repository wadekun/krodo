"""Tests for EditFileTool."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from krodo.core.workspace import LocalWorkspaceResolver
from krodo.sandbox.firewall import LocalSandboxRunner
from krodo.tools.builtin.fs import EditFileTool
from krodo.tools.protocols import ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    ws = LocalWorkspaceResolver().resolve(explicit=tmp_path)
    sb = LocalSandboxRunner(ws)
    return ToolContext(
        workspace=ws,
        sandbox=sb,
        session_id="test",
        logger=logging.getLogger("test"),
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_file_basic_replace(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("x = 1\ny = 2\n")
    ctx = _ctx(tmp_path)
    result = await EditFileTool().execute(
        {"path": "hello.py", "old_string": "x = 1", "new_string": "x = 42"},
        ctx,
    )
    assert not result.is_error, result.content
    assert (tmp_path / "hello.py").read_text() == "x = 42\ny = 2\n"


@pytest.mark.asyncio
async def test_edit_file_multiline_replace(tmp_path: Path) -> None:
    original = "def foo():\n    pass\n"
    (tmp_path / "f.py").write_text(original)
    ctx = _ctx(tmp_path)
    result = await EditFileTool().execute(
        {
            "path": "f.py",
            "old_string": "def foo():\n    pass",
            "new_string": "def foo():\n    return 1",
        },
        ctx,
    )
    assert not result.is_error, result.content
    assert "return 1" in (tmp_path / "f.py").read_text()


@pytest.mark.asyncio
async def test_edit_file_replace_all(tmp_path: Path) -> None:
    (tmp_path / "t.txt").write_text("foo foo foo\n")
    ctx = _ctx(tmp_path)
    result = await EditFileTool().execute(
        {"path": "t.txt", "old_string": "foo", "new_string": "bar", "replace_all": True},
        ctx,
    )
    assert not result.is_error, result.content
    assert (tmp_path / "t.txt").read_text() == "bar bar bar\n"
    assert "3 occurrence(s)" in result.content


@pytest.mark.asyncio
async def test_edit_file_replace_first_occurrence_only(tmp_path: Path) -> None:
    (tmp_path / "t.txt").write_text("a\na\na\n")
    ctx = _ctx(tmp_path)
    result = await EditFileTool().execute(
        {"path": "t.txt", "old_string": "a\n", "new_string": "b\n", "replace_all": False},
        ctx,
    )
    # With replace_all=False, "a\n" appears 3x so should fail uniqueness check
    assert result.is_error
    assert "3 times" in result.content


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_file_not_found(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await EditFileTool().execute(
        {"path": "nonexistent.py", "old_string": "x", "new_string": "y"},
        ctx,
    )
    assert result.is_error
    assert "does not exist" in result.content


@pytest.mark.asyncio
async def test_edit_file_old_string_not_found(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("print('hello')\n")
    ctx = _ctx(tmp_path)
    result = await EditFileTool().execute(
        {"path": "f.py", "old_string": "DOES_NOT_EXIST", "new_string": "x"},
        ctx,
    )
    assert result.is_error
    assert "not found" in result.content


@pytest.mark.asyncio
async def test_edit_file_ambiguous_old_string_reports_lines(tmp_path: Path) -> None:
    content = "import os\nimport os\nimport sys\n"
    (tmp_path / "f.py").write_text(content)
    ctx = _ctx(tmp_path)
    result = await EditFileTool().execute(
        {"path": "f.py", "old_string": "import os", "new_string": "import pathlib"},
        ctx,
    )
    assert result.is_error
    assert "2 times" in result.content
    # Should report line numbers
    assert "lines containing match" in result.content


@pytest.mark.asyncio
async def test_edit_file_path_traversal_denied(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await EditFileTool().execute(
        {"path": "../../../etc/passwd", "old_string": "root", "new_string": "evil"},
        ctx,
    )
    assert result.is_error
    assert "outside workspace" in result.content


@pytest.mark.asyncio
async def test_edit_file_on_directory_denied(tmp_path: Path) -> None:
    (tmp_path / "subdir").mkdir()
    ctx = _ctx(tmp_path)
    result = await EditFileTool().execute(
        {"path": "subdir", "old_string": "x", "new_string": "y"},
        ctx,
    )
    assert result.is_error
    assert "not a regular file" in result.content
