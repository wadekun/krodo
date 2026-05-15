"""Tests for ReadFileTool and WriteFileTool."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from coda.core.workspace import LocalWorkspaceResolver
from coda.sandbox.firewall import LocalSandboxRunner
from coda.tools.builtin.fs import ReadFileTool, WriteFileTool
from coda.tools.protocols import ToolContext


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
# ReadFileTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_file_happy_path(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text("print('hi')\n")
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": "hello.py"}, ctx)
    assert not result.is_error
    assert "print('hi')" in result.content


@pytest.mark.asyncio
async def test_read_file_missing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": "nope.py"}, ctx)
    assert result.is_error
    assert "does not exist" in result.content


@pytest.mark.asyncio
async def test_read_file_path_traversal(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": "../../../etc/passwd"}, ctx)
    assert result.is_error
    assert "outside workspace" in result.content


@pytest.mark.asyncio
async def test_read_file_absolute_path_denied(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": "/etc/passwd"}, ctx)
    assert result.is_error
    assert "outside workspace" in result.content


@pytest.mark.asyncio
async def test_read_file_symlink_escape_denied(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_read"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    link = tmp_path / "link.txt"
    link.symlink_to(outside / "secret.txt")
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": "link.txt"}, ctx)
    assert result.is_error
    assert "outside workspace" in result.content


@pytest.mark.asyncio
async def test_read_file_with_offset_limit(tmp_path: Path) -> None:
    lines = [f"line {i}\n" for i in range(1, 11)]
    (tmp_path / "file.txt").write_text("".join(lines))
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": "file.txt", "offset": 3, "limit": 2}, ctx)
    assert not result.is_error
    assert "line 3" in result.content
    assert "line 4" in result.content
    assert "line 5" not in result.content


@pytest.mark.asyncio
async def test_read_file_not_a_file(tmp_path: Path) -> None:
    (tmp_path / "subdir").mkdir()
    ctx = _ctx(tmp_path)
    result = await ReadFileTool().execute({"path": "subdir"}, ctx)
    assert result.is_error
    assert "not a regular file" in result.content


# ---------------------------------------------------------------------------
# WriteFileTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_file_creates_new(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await WriteFileTool().execute({"path": "out.py", "content": "x = 1\n"}, ctx)
    assert not result.is_error
    assert (tmp_path / "out.py").read_text() == "x = 1\n"


@pytest.mark.asyncio
async def test_write_file_creates_directories(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await WriteFileTool().execute(
        {"path": "deep/nested/file.txt", "content": "hello"}, ctx
    )
    assert not result.is_error
    assert (tmp_path / "deep" / "nested" / "file.txt").exists()


@pytest.mark.asyncio
async def test_write_file_traversal_denied(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await WriteFileTool().execute(
        {"path": "../../../tmp/evil.sh", "content": "rm -rf /"}, ctx
    )
    assert result.is_error
    assert "outside workspace" in result.content


@pytest.mark.asyncio
async def test_write_file_absolute_path_denied(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = await WriteFileTool().execute({"path": "/tmp/evil.sh", "content": "evil"}, ctx)
    assert result.is_error


@pytest.mark.asyncio
async def test_write_file_overwrites_existing(tmp_path: Path) -> None:
    (tmp_path / "existing.txt").write_text("old content")
    ctx = _ctx(tmp_path)
    result = await WriteFileTool().execute({"path": "existing.txt", "content": "new content"}, ctx)
    assert not result.is_error
    assert (tmp_path / "existing.txt").read_text() == "new content"
